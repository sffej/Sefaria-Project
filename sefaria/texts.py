# -*- coding: utf-8 -*-
"""
texts.py -- backend core for manipulating texts, refs (citations), links, notes and text index records.

MongoDB collections handled in this file: index, texts, links, notes, history
"""
import re
import copy

from sefaria.model.text import merge_texts
import sefaria.model as model
import summaries
from sefaria.utils.util import list_depth
from sefaria.utils.talmud import section_to_daf
from sefaria.system.database import db
import sefaria.system.cache as scache
from sefaria.system.exceptions import InputError

import logging
logger = logging.getLogger(__name__)


#Move to Ref.version_list()
def get_version_list(tref):
    """
    Returns a list of available text versions matching 'ref'
    """
    try:
        oref = model.Ref(tref).padded_ref()
    except InputError:
        return []
    #pRef = parse_ref(tref)
    #if "error" in pRef:
    #	return []

    skip = oref.sections[0] - 1 if len(oref.sections) else 0
    limit = 1
    versions = db.texts.find({"title": oref.book}, {"chapter": {"$slice": [skip, limit]}})

    vlist = []
    for v in versions:
        text = v['chapter']
        for i in [0] + oref.sections[1:]:
            try:
                text = text[i]
            except (IndexError, TypeError):
                text = None
                continue
        if text:
            vlist.append({"versionTitle": v["versionTitle"], "language": v["language"]})

    return vlist


def get_book_link_collection(book, cat):

    if cat == "Tanach" or cat == "Torah" or cat == "Prophets" or cat == "Writings":
        query = {"$and": [{"categories": cat}, {"categories": {"$ne": "Commentary"}}, {"categories": {"$ne": "Targum"}}]}
    else:
        query = {"categories": cat}

    titles = model.IndexSet(query).distinct("title")
    if len(titles) == 0:
        return {"error": "No results for {}".format(query)}

    book_re = r'^{} \d'.format(book)
    cat_re = r'^({}) \d'.format('|'.join(titles))

    link_re = r'^(?P<title>.+) (?P<loc>\d.*)$'
    ret = []

    links = model.LinkSet({"$and": [{"refs": {"$regex": book_re}}, {"refs": {"$regex": cat_re}}]})
    for link in links:
        l1 = re.match(link_re, link.refs[0])
        l2 = re.match(link_re, link.refs[1])
        ret.append({
            "r1": {"title": l1.group("title").replace(" ", "-"), "loc": l1.group("loc")},
            "r2": {"title": l2.group("title").replace(" ", "-"), "loc": l2.group("loc")}
        })
    return ret

# No usages found
def merge_text(a, b):
    """
    Merge two lists representing texts, giving preference to a, but keeping
    values froms b when a position in a is empty or non existant.

    e.g merge_text(["", "Two", "Three"], ["One", "Nope", "Nope", "Four]) ->
        ["One", "Two" "Three", "Four"]
    """
    length = max(len(a), len(b))
    out = [a[n] if n < len(a) and (a[n] or not n < len(b)) else b[n] for n in range(length)]
    return out


# views.lock_text_api
def set_text_version_status(title, lang, version, status=None):
    """
    Sets the status field of an existing text version.
    """
    title   = title.replace("_", " ")
    version = version.replace("_", " ")
    text = db.texts.find_one({"title": title, "language": lang, "versionTitle": version})
    if not text:
        return {"error": "Text not found: %s, %s, %s" % (title, lang, version)}

    text["status"] = status
    db.texts.save(text)
    return {"status": "ok"}


#only used in a script
def update_version_title(old, new, text_title, language):
    """
    Rename a text version title, including versions in history
    'old' and 'new' are the version title names.
    """
    query = {
        "title": text_title,
        "versionTitle": old,
        "language": language
    }
    db.texts.update(query, {"$set": {"versionTitle": new}}, upsert=False, multi=True)

    update_version_title_in_history(old, new, text_title, language)


def update_version_title_in_history(old, new, text_title, language):
    """
    Rename a text version title in history records
    'old' and 'new' are the version title names.
    """
    query = {
        "ref": {"$regex": r'^%s(?= \d)' % text_title},
        "version": old,
        "language": language,
    }
    db.history.update(query, {"$set": {"version": new}}, upsert=False, multi=True)


#only used in a script
def merge_text_versions(version1, version2, text_title, language, warn=False):
    """
    Merges the contents of two distinct text versions.
    version2 is merged into version1 then deleted.
    Preference is giving to version1 - if both versions contain content for a given segment,
    only the content of version1 will be retained.


    History entries are rewritten for version2.
    NOTE: the history of that results will be incorrect for any case where the content of
    version2 is overwritten - the history of those overwritten edits will remain.
    To end with a perfectly accurate history, history items for segments which have been overwritten
    would need to be identified and deleted.
    """
    v1 = model.Version().load({"title": text_title, "versionTitle": version1, "language": language})
    if not v1:
        return {"error": "Version not found: %s" % version1 }
    v2 = model.Version().load({"title": text_title, "versionTitle": version2, "language": language})
    if not v2:
        return {"error": "Version not found: %s" % version2 }

    if warn and versions_overlap(v1.chapter, v2.chapter):
        print "WARNING - %s & %s have overlapping content. Aborting." % (version1, version2)


    merged_text, sources = merge_texts([v1.chapter, v2.chapter], [version1, version2])

    v1.chapter = merged_text
    v1.save()

    update_version_title_in_history(version2, version1, text_title, language)

    v2.delete()

    return {"status": "ok"}


def merge_multiple_text_versions(versions, text_title, language, warn=False):
    """
    Merges contents of multiple text versions listed in 'versions'
    Versions listed first in 'versions' will receive priority if there is overlap.
    """
    v1 = versions.pop(0)
    for v2 in versions:
        merge_text_versions(v1, v2, text_title, language)


def merge_text_versions_by_source(text_title, language, warn=False):
    """
    Merges all texts of text_title in langauge that share the same value for versionSource.
    """
    v = model.VersionSet({"title": text_title, "language": language})

    for s in v.distinct("versionSource"):
        versions = model.VersionSet({"title": text_title, "versionSource": s, "language": language}).distinct("versionTitle")
        merge_multiple_text_versions(versions, text_title, language)


def merge_text_versions_by_language(text_title, language, warn=False):
    """
    Merges all texts of text_title in langauge.
    """
    versions = model.VersionSet({"title": text_title, "language": language}).distinct("versionTitle")
    merge_multiple_text_versions(versions, text_title, language)


def versions_overlap(v1, v2):
    """
    Returns True if jagged text arrrays v1 & v2 contain one or more positions where both are non empty.
    Runs recursively.
    """
    if isinstance(v1, list) and isinstance(v2, list):
        for i in range(min(len(v1), len(v2))):
            if versions_overlap(v1[i], v2[i]):
                return True
    if isinstance(v1, basestring) and isinstance(v2, basestring):
        if v1 and v2:
            return True
    return False


def rename_category(old, new):
    """
    Walk through all index records, replacing every category instance
    called 'old' with 'new'.
    """
    indices = model.IndexSet({"categories": old})

    assert indices.count(), "No categories named {}".format(old)

    for i in indices:
        i.categories = [new if cat == old else cat for cat in i.categories]
        i.save()

    summaries.update_summaries()


def resize_text(title, new_structure, upsize_in_place=False):
    # todo: Needs to be converted to objects, but no usages seen in the wild.
    """
    Change text structure for text named 'title'
    to 'new_structure' (a list of strings naming section names)

    Changes index record as well as restructuring any text that is currently saved.

    When increasing size, any existing text will become the first segment of the new level
    ["One", "Two", "Three"] -> [["One"], ["Two"], ["Three"]]

    If upsize_in_place==True, existing text will stay in tact, but be wrapped in new depth:
    ["One", "Two", "Three"] -> [["One", "Two", "Three"]]

    When decreasing size, information is lost as any existing segments are concatenated with " "
    [["One1", "One2"], ["Two1", "Two2"], ["Three1", "Three2"]] - >["One1 One2", "Two1 Two2", "Three1 Three2"]

    """
    index = db.index.find_one({"title": title})
    if not index:
        return False

    old_structure = index["sectionNames"]
    index["sectionNames"] = new_structure
    db.index.save(index)

    delta = len(new_structure) - len(old_structure)
    if delta == 0:
        return True

    texts = db.texts.find({"title": title})
    for text in texts:
        if delta > 0 and upsize_in_place:
            resized = text["chapter"]
            for i in range(delta):
                resized = [resized]
        else:
            resized = resize_jagged_array(text["chapter"], delta)

        text["chapter"] = resized
        db.texts.save(text)

    # TODO Rewrite any existing Links
    # TODO Rewrite any exisitng History items

    summaries.update_summaries_on_change(title)
    scache.reset_texts_cache()

    return True


def resize_jagged_array(text, factor):
    """
    Return a resized jagged array for 'text' either up or down by int 'factor'.
    Size up if factor is positive, down if negative.
    Size up or down the number of times per factor's size.
    E.g., up twice for '2', down twice for '-2'.
    """
    new_text = text
    if factor > 0:
        for i in range(factor):
            new_text = upsize_jagged_array(new_text)
    elif factor < 0:
        for i in range(abs(factor)):
            new_text = downsize_jagged_array(new_text)

    return new_text


def upsize_jagged_array(text):
    """
    Returns a jagged array for text which restructures the content of text
    to include one additional level of structure.
    ["One", "Two", "Three"] -> [["One"], ["Two"], ["Three"]]
    """
    new_text = []
    for segment in text:
        if isinstance(segment, basestring):
            new_text.append([segment])
        elif isinstance(segment, list):
            new_text.append(upsize_jagged_array(segment))

    return new_text


def downsize_jagged_array(text):
    """
    Returns a jagged array for text which restructures the content of text
    to include one less level of structure.
    Existing segments are concatenated with " "
    [["One1", "One2"], ["Two1", "Two2"], ["Three1", "Three2"]] - >["One1 One2", "Two1 Two2", "Three1 Three2"]
    """
    new_text = []
    for segment in text:
        # Assumes segments are of uniform type, either all strings or all lists
        if isinstance(segment, basestring):
            return " ".join(text)
        elif isinstance(segment, list):
            new_text.append(downsize_jagged_array(segment))

    # Return which was filled in, defaulted to [] if both are empty
    return new_text


# move to JaggedArray?
def grab_section_from_text(sections, text, toSections=None):
    """
    Returns a section of text from within the jagged array 'text'
    that is denoted by sections and toSections.
    """
    if len(sections) == 0:
        return text
    if not text:
        return ""

    toSections = toSections or sections
    try:
        if sections[0] == toSections[0]:
            if len(sections) == 1:
                return text[sections[0]-1]
            else:
                return grab_section_from_text(sections[1:], text[sections[0]-1], toSections[1:])
        else:
            return text[ sections[0]-1 : toSections[0]-1 ]

    except IndexError:
        # Index out of bounds, we don't have this text
        return ""
    except TypeError:
        return ""
