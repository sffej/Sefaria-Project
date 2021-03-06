# -*- coding: utf-8 -*-

from sefaria.model import *
from sefaria.model.abstract import AbstractMongoRecord
from sefaria.system.exceptions import InputError
from sefaria.system.database import db
from sefaria.sheets import save_sheet
import re

"""
Experimental
These utilities have been used a few times, but are still rough.

To get the existing schema nodes to pass into these functions, easiest is likely:
Ref("...").index_node


Todo:
    Clean system from old refs:
        links to commentary
        transx reqs
        elastic search
        varnish
"""


def insert_last_child(new_node, parent_node):
    return attach_branch(new_node, parent_node, len(parent_node.children))


def insert_first_child(new_node, parent_node):
    return attach_branch(new_node, parent_node, 0)


def attach_branch(new_node, parent_node, place=0):
    """
    :param new_node: A schema node tree to attach
    :param parent_node: The parent to attach it to
    :param place: The index of the child before which to insert, so place=0 inserts at the front of the list, and place=len(parent_node.children) inserts at the end
    :return:
    """
    assert isinstance(new_node, SchemaNode)
    assert isinstance(parent_node, SchemaNode)
    assert place <= len(parent_node.children)

    index = parent_node.index

    # Add node to versions & commentary versions
    vs = [v for v in index.versionSet()]
    vsc = [v for v in library.get_commentary_versions_on_book(index.title)]
    for v in vs + vsc:
        pc = v.content_node(parent_node)
        pc[new_node.key] = new_node.create_skeleton()
        v.save(override_dependencies=True)

    # Update Index schema and save
    parent_node.children.insert(place, new_node)
    new_node.parent = parent_node

    index.save(override_dependencies=True)
    library.rebuild()
    refresh_version_state(index.title)


def remove_branch(node):
    """
    This will delete any text in `node`
    :param node: SchemaNode to remove
    :return:
    """
    assert isinstance(node, SchemaNode)
    parent = node.parent
    assert parent
    index = node.index

    node.ref().linkset().delete()
    # todo: commentary linkset

    vs = [v for v in index.versionSet()]
    vsc = [v for v in library.get_commentary_versions_on_book(index.title)]
    for v in vs + vsc:
        assert isinstance(v, Version)
        pc = v.content_node(parent)
        del pc[node.key]
        v.save(override_dependencies=True)

    parent.children = [n for n in parent.children if n.key != node.key]

    index.save(override_dependencies=True)
    library.rebuild()
    refresh_version_state(index.title)


def reorder_children(parent_node, new_order):
    """
    :param parent_node:
    :param new_order: List of child keys, in their new order
    :return:
    """
    # With this one, we can get away with just an Index change
    assert isinstance(parent_node, SchemaNode)
    child_dict = {n.key: n for n in parent_node.children}
    assert set(child_dict.keys()) == set(new_order)
    parent_node.children = [child_dict[k] for k in new_order]
    parent_node.index.save()


def merge_default_into_parent(parent_node):
    """
    In a case where a parent has only one child - a default child - this merges the two together into one Jagged Array node.

    Example Usage:
    >>> r = Ref('Mei HaShiloach, Volume II, Prophets, Judges')
    >>> merge_default_into_parent(r.index_node)

    :param parent_node:
    :return:
    """
    assert isinstance(parent_node, SchemaNode)
    assert len(parent_node.children) == 1
    assert parent_node.has_default_child()
    default_node = parent_node.get_default_child()
    #assumption: there's a grandparent.  todo: handle the case where the parent is the root node of the schema
    is_root = True
    if parent_node.parent:
        is_root = False
        grandparent_node = parent_node.parent
    index = parent_node.index

    # Repair all versions
    vs = [v for v in index.versionSet()]
    vsc = [v for v in library.get_commentary_versions_on_book(index.title)]
    for v in vs + vsc:
        assert isinstance(v, Version)
        if is_root:
            v.chapter = v.chapter["default"]
        else:
            grandparent_version_dict = v.sub_content(grandparent_node.version_address())
            grandparent_version_dict[parent_node.key] = grandparent_version_dict[parent_node.key]["default"]
        v.save(override_dependencies=True)

    # Rebuild Index
    new_node = JaggedArrayNode()
    new_node.key = parent_node.key
    new_node.title_group = parent_node.title_group
    new_node.sectionNames = default_node.sectionNames
    new_node.addressTypes = default_node.addressTypes
    new_node.depth = default_node.depth
    if is_root:
        index.nodes = new_node
    else:
        grandparent_node.children = [c if c.key != parent_node.key else new_node for c in grandparent_node.children]

    # Save index and rebuild library
    index.save(override_dependencies=True)
    library.rebuild()
    refresh_version_state(index.title)


def convert_simple_index_to_complex(index):
    """
    The target complex text will have a 'default' node.
    All refs to this text should remain good.
    :param index:
    :return:
    """
    from sefaria.model.schema import TitleGroup

    assert isinstance(index, Index)

    ja_node = index.nodes
    assert isinstance(ja_node, JaggedArrayNode)

    # Repair all version
    vs = [v for v in index.versionSet()]
    vsc = [v for v in library.get_commentary_versions_on_book(index.title)]
    for v in vs + vsc:
        assert isinstance(v, Version)
        v.chapter = {"default": v.chapter}
        v.save(override_dependencies=True)

    # Build new schema
    new_parent = SchemaNode()
    new_parent.title_group = ja_node.title_group
    new_parent.key = ja_node.key
    ja_node.title_group = TitleGroup()
    ja_node.key = "default"
    ja_node.default = True

    # attach to index record
    new_parent.append(ja_node)
    index.nodes = new_parent

    index.save(override_dependencies=True)
    library.rebuild()
    refresh_version_state(index.title)


def change_parent(node, new_parent, place=0):
    """
    :param node:
    :param new_parent:
    :param place: The index of the child before which to insert, so place=0 inserts at the front of the list, and place=len(parent_node.children) inserts at the end
    :return:
    """
    assert isinstance(node, SchemaNode)
    assert isinstance(new_parent, SchemaNode)
    assert place <= len(new_parent.children)
    old_parent = node.parent
    index = new_parent.index

    old_normal_form = node.ref().normal()
    linkset = [l for l in node.ref().linkset()]

    vs = [v for v in index.versionSet()]
    vsc = [v for v in library.get_commentary_versions_on_book(index.title)]
    for v in vs + vsc:
        assert isinstance(v, Version)
        old_parent_content = v.content_node(old_parent)
        content = old_parent_content.pop(node.key)
        new_parent_content = v.content_node(new_parent)
        new_parent_content[node.key] = content
        v.save(override_dependencies=True)

    old_parent.children = [n for n in old_parent.children if n.key != node.key]
    new_parent.children.insert(place, node)
    node.parent = new_parent
    new_normal_form = node.ref().normal()

    index.save(override_dependencies=True)
    library.rebuild()

    for link in linkset:
        link.refs = [ref.replace(old_normal_form, new_normal_form) for ref in link.refs]
        link.save()
    # todo: commentary linkset

    refresh_version_state(index.title)


def refresh_version_state(base_title):
    """
    ** VersionState is *not* altered on Index save.  It is only created on Index creation.
    ^ It now seems that VersionState is referenced on Index save

    VersionState is *not* automatically updated on Version save.
    The VersionState update on version save happens in texts_api().
    VersionState.refresh() assumes the structure of content has not changed.
    To regenerate VersionState, we save the flags, delete the old one, and regenerate a new one.
    """
    vtitles = library.get_commentary_version_titles_on_book(base_title) + [base_title]
    for title in vtitles:
        vs = VersionState(title)
        flags = vs.flags
        vs.delete()
        VersionState(title, {"flags": flags})


def change_node_title(snode, old_title, lang, new_title):
    """
    Changes the title of snode specified by old_title and lang, to new_title.
    If the title changing is the primary english title, cascades to all of the impacted objects
    :param snode:
    :param old_title:
    :param lang:
    :param new_title:
    :return:
    """
    pass


def replaceBadNodeTitles(title, bad_char, good_char, lang):
    '''
    This recurses through the serialized tree changing replacing the previous title of each node to its title with the bad_char replaced by good_char. 
    '''
    def recurse(node):
        if 'nodes' in node:
            for each_one in node['nodes']:
                recurse(each_one)
        elif 'default' not in node:

            if 'title' in node:
                node['title'] = node['title'].replace(bad_char, good_char)
            if 'titles' in node:
                which_one = -1
                if node['titles'][0]['lang'] == lang:
                    which_one = 0
                elif len(node['titles']) > 1 and node['titles'][1]['lang'] == lang:
                    which_one = 1
                if which_one >= 0:
                    node['titles'][which_one]['text'] = node['titles'][which_one]['text'].replace(bad_char, good_char)
 
    data = library.get_index(title).nodes.serialize()
    recurse(data)
    return data


def change_node_structure(ja_node, section_names, address_types=None, upsize_in_place=False):
    """
    Updates the structure of a JaggedArrayNode to the depth specified by the length of sectionNames.

    When increasing size, any existing text will become the first segment of the new level
    ["One", "Two", "Three"] -> [["One"], ["Two"], ["Three"]]

    When decreasing size, information is lost as any existing segments are concatenated with " "
    [["One1", "One2"], ["Two1", "Two2"], ["Three1", "Three2"]] - >["One1 One2", "Two1 Two2", "Three1 Three2"]

    A depth 0 text (i.e. a single string or an empty list) will be treated as if upsize_in_place was set to True

    :param ja_node: JaggedArrayNode to be edited. Must be instance of class: JaggedArrayNode

    :param section_names: sectionNames parameter of restructured node. This determines the depth
    :param address_types: address_type parameter of restructured node. Defaults to ['Integer'] * len(sectionNames)

    :param upsize_in_place: If True, existing text will stay in tact, but be wrapped in new depth:
    ["One", "Two", "Three"] -> [["One", "Two", "Three"]]
    """

    assert isinstance(ja_node, JaggedArrayNode)
    assert len(section_names) > 0

    if hasattr(ja_node, 'lengths'):
        print 'WARNING: This node has predefined lengths!'
        del ja_node.lengths

    # `delta` is difference in depth.  If positive, we're adding depth.
    delta = len(section_names) - len(ja_node.sectionNames)
    if upsize_in_place:
        assert (delta > 0)

    if address_types is None:
        address_types = ['Integer'] * len(section_names)
    else:
        assert len(address_types) == len(section_names)

    def fix_ref(ref_string):
        """
        Takes a string from link.refs and updates to reflect the new structure.
        Uses the delta parameter from the main function to determine how to update the ref.
        `delta` is difference in depth.  If positive, we're adding depth.
        :param ref_string: A string which can be interpreted as a valid Ref
        :return: string
        """
        if delta == 0:
            return ref_string

        d = Ref(ref_string)._core_dict()

        if delta < 0: # Making node shallower
            for i in range(-delta):
                if len(d["sections"]) == 0:
                    break
                d["sections"].pop()
                d["toSections"].pop()

                # else, making node deeper
        elif upsize_in_place:
            for i in range(delta):
                d["sections"].insert(0, 1)
                d["toSections"].insert(0, 1)
        else:
            for i in range(delta):
                d["sections"].append(1)
                d["toSections"].append(1)
        return Ref(_obj=d).normal()

    commentators = library.get_commentary_version_titles_on_book(ja_node.index.title)
    commentators = [c.replace(u' on {}'.format(ja_node.ref().normal()), u'') for c in commentators]
    ref_regex_str = ja_node.ref().regex(anchored=False)
    identifier = ur"(^{})|(^({}) on {})".format(ref_regex_str, "|".join(commentators), ref_regex_str)

    def needs_fixing(ref_string):
        if re.search(identifier, ref_string) is None:
            return False
        else:
            return True

    # For downsizing, refs will become invalidated in their current state, so changes must be made before the
    # structure change.
    if delta < 0:
        cascade(ja_node.ref(), rewriter=fix_ref, needs_rewrite=needs_fixing)
        # cascade updates the index record, ja_node index gets stale
        ja_node.index = library.get_index(ja_node.index.title)

    ja_node.sectionNames = section_names
    ja_node.addressTypes = address_types
    ja_node.depth = len(section_names)
    index = ja_node.index
    index.save(override_dependencies=True)
    print 'Index Saved'
    library.refresh_index_record_in_cache(index)
    # ensure the index on the ja_node object is updated with the library refresh
    ja_node.index = library.get_index(ja_node.index.title)

    vs = [v for v in index.versionSet()]
    vsc = [v for v in library.get_commentary_versions_on_book(index.title)]
    print 'Updating Versions'
    for v in vs + vsc:
        assert isinstance(v, Version)
        if v.get_index() == index:
            chunk = TextChunk(ja_node.ref(), lang=v.language, vtitle=v.versionTitle)
        else:
            library.refresh_index_record_in_cache(v.get_index())
            ref_name = ja_node.ref().normal()
            ref_name = ref_name.replace(index.title, v.get_index().title)
            chunk = TextChunk(Ref(ref_name), lang=v.language, vtitle=v.versionTitle)
        ja = chunk.ja()

        if upsize_in_place or ja.get_depth() == 0:
            wrapper = chunk.text
            for i in range(delta):
                wrapper = [wrapper]
            chunk.text = wrapper
            chunk.save()

        else:
            chunk.text = ja.resize(delta).array()
            chunk.save()

    # For upsizing, we are editing refs to a structure that would not be valid till after the change, therefore
    # cascading must be performed here
    if delta > 0:
        cascade(ja_node.ref(), rewriter=fix_ref, needs_rewrite=needs_fixing)

    library.rebuild()
    refresh_version_state(index.title)
    # For each commentary version, refresh its VS


def cascade(ref_identifier, rewriter=lambda x: x, needs_rewrite=lambda x: True):
    """
    Changes to indexes requires updating any and all data that reference that index. This routine will take a rewriter
     function an run it on every location that references the updated index.
    :param ref_identifier: Ref or String that can be used to implement a ref
    :param rewriter: callback function used to update the field
    :param needs_rewrite: Criteria for which a save will be triggered. If not set, routine will trigger a save for
    every item within the set
    :param skip_history: Set to True to skip history updates
    """

    def generic_rewrite(model_set, attr_name='ref', sub_attr_name=None,):
        """
        Generic routine to take any derivative of AbstractMongoSet and update the fields outlined by attr_name using
        the callback function rewriter.

        This routine is heavily inspired by Splicer._generic_set_rewrite
        :param model_set: Derivative of AbstractMongoSet
        :param attr_name: name of attribute to update
        :param sub_attr_name: Use to update nested attributes
        :return:
        """

        for record in model_set:
            assert isinstance(record, AbstractMongoRecord)
            if sub_attr_name is None:
                refs = getattr(record, attr_name)
            else:
                intermediate_obj = getattr(record, attr_name)
                refs = intermediate_obj[sub_attr_name]

            if isinstance(refs, list):
                needs_save = False
                for ref_num, ref in enumerate(refs):
                    if needs_rewrite(ref):
                        needs_save = True
                        refs[ref_num] = rewriter(ref)
                if needs_save:
                    try:
                        record.save()
                    except InputError as e:
                        print 'Bad Data Found: {}'.format(refs)
                        print e
            else:
                if needs_rewrite(refs):
                    refs = rewriter(refs)
                    try:
                        record.save()
                    except InputError as e:
                        print 'Bad Data Found: {}'.format(refs)
                        print e

    def clean_sheets(sheets_to_update):

        def rewrite_source(source):
            requires_save = False
            if "ref" in source:
                try:
                    ref = Ref(source["ref"])
                except InputError as e:
                    print "Error: In _clean_sheets.rewrite_source: failed to instantiate Ref {}".format(source["ref"])
                else:
                    if needs_rewrite(source['ref']):
                        requires_save = True
                        source["ref"] = rewriter(source['ref'])
            if "subsources" in source:
                for subsource in source["subsources"]:
                    requires_save = rewrite_source(subsource) or requires_save
            return requires_save

        for sid in sheets_to_update:
            needs_save = False
            sheet = db.sheets.find_one({"id": sid})
            if not sheet:
                print "Likely error - can't load sheet {}".format(sid)
            for source in sheet["sources"]:
                if rewrite_source(source):
                    needs_save = True
            if needs_save:
                sheet["lastModified"] = sheet["dateModified"]
                save_sheet(sheet, sheet["owner"])

    def update_alt_structs(index):

        assert isinstance(index, Index)
        if not index.has_alt_structures():
            return
        needs_save = False

        for name, struct in index.get_alt_structures().iteritems():
            for map_node in struct.get_leaf_nodes():
                assert map_node.depth <= 1, "Need to write some code to handle alt structs with depth > 1!"
                wr = map_node.wholeRef
                if needs_rewrite(wr):
                    needs_save = True
                    map_node.wholeRef = rewriter(wr)
                if hasattr(map_node, 'refs'):
                    for ref_num, ref in enumerate(map_node.refs):
                        if needs_rewrite(ref):
                            needs_save = True
                            map_node.refs[ref_num] = rewriter(ref)
        if needs_save:
            index.save()

    if isinstance(ref_identifier, basestring):
        ref_identifier = Ref(ref_identifier)
    assert isinstance(ref_identifier, Ref)

    commentators = library.get_commentary_version_titles_on_book(ref_identifier.book)
    commentators = [item for c in commentators for item in Ref(c).regex(as_list=True)]
    ref_regex = ref_identifier.regex(anchored=False, as_list=True)
    identifier = ref_regex + commentators
    # titles = re.compile(identifier)

    def construct_query(attribute, queries):

        query_list = [{attribute: {'$regex': '^'+query}} for query in queries]
        return {'$or': query_list}

    print 'Updating Links'
    generic_rewrite(LinkSet(construct_query('refs', identifier)), attr_name='refs')
    print 'Updating Notes'
    generic_rewrite(NoteSet(construct_query('ref', identifier)))
    generic_rewrite(TranslationRequestSet(construct_query('ref', identifier)))
    print 'Updatding Sheets'
    clean_sheets([s['id'] for s in db.sheets.find(construct_query('sources.ref', identifier), {"id": 1})])
    print 'Updating Alternate Structs'
    update_alt_structs(ref_identifier.index)
    print 'Updating History'
    generic_rewrite(HistorySet(construct_query('ref', identifier)))
    generic_rewrite(HistorySet(construct_query('new.ref', identifier)), attr_name='new', sub_attr_name='ref')
    generic_rewrite(HistorySet(construct_query('new.refs', identifier)), attr_name='new', sub_attr_name='refs')
    generic_rewrite(HistorySet(construct_query('old.ref', identifier)), attr_name='old', sub_attr_name='ref')
    generic_rewrite(HistorySet(construct_query('old.refs', identifier)), attr_name='old', sub_attr_name='refs')
