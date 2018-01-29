import logging
import json
import re
from itertools import chain

from future.utils import python_2_unicode_compatible

import six
from six.moves.configparser import ConfigParser # pylint: disable=E0401

from lxml import etree

from sciencebeam_gym.utils.string import (
  LazyStr
)

from sciencebeam_gym.utils.xml import (
  get_text_content,
  get_immediate_text
)

from sciencebeam_gym.utils.collection import (
  filter_truthy,
  strip_all
)

def get_logger():
  return logging.getLogger(__name__)

class XmlMappingSuffix(object):
  REGEX = '.regex'
  MATCH_MULTIPLE = '.match-multiple'
  BONDING = '.bonding'
  REQUIRE_NEXT = '.require-next'
  CHILDREN = '.children'
  CHILDREN_CONCAT = '.children.concat'
  CHILDREN_RANGE = '.children.range'
  UNMATCHED_PARENT_TEXT = '.unmatched-parent-text'
  PRIORITY = '.priority'
  SUB = '.sub'

@python_2_unicode_compatible
class TargetAnnotation(object):
  def __init__(
    self, value, name,
    match_multiple=False, bonding=False, require_next=False,
    sub_annotations=None):

    self.value = value
    self.name = name
    self.match_multiple = match_multiple
    self.bonding = bonding
    self.require_next = require_next
    self.sub_annotations = sub_annotations

  def __str__(self):
    return u'{} (match_multiple={}, bonding={}, require_next={}, sub_annotations={}): {}'.format(
      self.name, self.match_multiple, self.bonding, self.require_next, self.sub_annotations,
      self.value
    )


def parse_xml_mapping(xml_mapping_filename):
  with open(xml_mapping_filename, 'r') as f:
    config = ConfigParser()
    if six.PY3:
      config.read_file(f)
    else:
      config.readfp(f)
    return {
      k: dict(config.items(k))
      for k in config.sections()
    }

def relace_recursive(s, old, new):
  previous = None
  while s != previous:
    previous = s
    s = s.replace(old, new)
  return s

def strip_whitespace(s):
  replacements = [
    ('\t', ' '),
    ('  ', ' '),
    ('\r', '\n'),
    (' \n', '\n'),
    ('\n ', '\n'),
    ('\n\n', '\n')
  ]
  for old, new in replacements:
    s = relace_recursive(s, old, new)
  return s

def get_stripped_text_content(node, **kwargs):
  return strip_whitespace(get_text_content(node, **kwargs).strip())

def get_stripped_text_content_list(nodes, **kwargs):
  return [get_stripped_text_content(node, **kwargs) for node in nodes]

def apply_pattern(s, compiled_pattern):
  m = compiled_pattern.match(s)
  if m:
    get_logger().debug('regex match: %s -> %s', compiled_pattern, m.groups())
    return m.group(1)
  return s

def iter_parents(children):
  for child in children:
    p = child.getparent()
    if p is not None:
      yield p

def exclude_parents(children):
  if not isinstance(children, list):
    children = list(children)
  all_parents = set(iter_parents(children))
  return [child for child in children if not child in all_parents]

def extract_children_source_list(parent, children_source_list):
  used_nodes = set()
  values = []
  for children_source in children_source_list:
    xpath = children_source.get('xpath')
    if xpath:
      matching_nodes = exclude_parents(parent.xpath(xpath))
      if not matching_nodes:
        get_logger().debug(
          'child xpath does not match any item, skipping: xpath=%s (xml=%s)',
          xpath,
          LazyStr(lambda: str(etree.tostring(parent)))
        )
        used_nodes = set()
        values = []
        break
      used_nodes |= set(matching_nodes)
      value = ' '.join(get_stripped_text_content_list(matching_nodes))
    else:
      value = children_source.get('value')
    values.append(value or '')
  return values, used_nodes

def extract_children_concat(parent, children_concat):
  used_nodes = set()
  values = []
  get_logger().debug('children_concat: %s', children_concat)
  for children_concat_item in children_concat:
    temp_values, temp_used_nodes = extract_children_source_list(
      parent, children_concat_item
    )
    used_nodes |= temp_used_nodes
    if temp_values:
      values.append(''.join(temp_values))
  return values, used_nodes

def extract_children_range(parent, children_range):
  used_nodes = set()
  values = []
  standalone_values = []
  get_logger().debug('children_range: %s', children_range)
  for range_item in children_range:
    temp_values, temp_used_nodes = extract_children_source_list(
      parent, [range_item.get('min'), range_item.get('max')]
    )
    if len(temp_values) == 2:
      temp_values = strip_all(temp_values)
      if all(s.isdigit() for s in temp_values):
        num_values = [int(s) for s in temp_values]
        range_values = [str(x) for x in range(num_values[0], num_values[1] + 1)]
        if range_item.get('standalone'):
          standalone_values.extend(range_values)
        else:
          values.extend(range_values)
        used_nodes |= temp_used_nodes
      else:
        get_logger().info('values not integers: %s', temp_values)
  return values, standalone_values, used_nodes

def parse_xpaths(s):
  return strip_all(s.strip().split('\n')) if s else None

def match_xpaths(parent, xpaths):
  return chain(*[parent.xpath(s) for s in xpaths])

def extract_children(
  parent, children_xpaths, children_concat, children_range, unmatched_parent_text):

  concat_values_list, concat_used_nodes = extract_children_concat(parent, children_concat)
  range_values_list, standalone_values, range_used_nodes = (
    extract_children_range(parent, children_range)
  )
  used_nodes = concat_used_nodes | range_used_nodes

  other_child_nodes = [
    node for node in match_xpaths(parent, children_xpaths)
    if not node in used_nodes
  ]
  other_child_nodes_excl_parents = exclude_parents(other_child_nodes)
  text_content_list = filter_truthy(strip_all(
    get_stripped_text_content_list(other_child_nodes_excl_parents) +
    concat_values_list + range_values_list
  ))
  if len(other_child_nodes_excl_parents) != len(other_child_nodes):
    other_child_nodes_excl_parents_set = set(other_child_nodes_excl_parents)
    for child in other_child_nodes:
      if child not in other_child_nodes_excl_parents_set:
        text_values = filter_truthy(strip_all(get_immediate_text(child)))
        text_content_list.extend(text_values)
  if unmatched_parent_text:
    value = get_stripped_text_content(
      parent,
      exclude=set(other_child_nodes) | used_nodes
    ).strip()
    if value and not value in text_content_list:
      text_content_list.append(value)
  return text_content_list, standalone_values

def parse_json_with_default(s, default_value):
  return json.loads(s) if s else default_value

def get_prefixed_dict_values(d, key_prefix):
  return {
    k[len(key_prefix):]: v
    for k, v in d.items()
    if k.startswith(key_prefix)
  }

def get_sub_mapping(mapping, tag):
  return get_prefixed_dict_values(mapping, tag + XmlMappingSuffix.SUB + '.')

def extract_sub_annotations(parent_node, sub_xpaths):
  if not sub_xpaths:
    return
  sub_annotations = []
  for sub_tag, sub_xpath in sub_xpaths.items():
    for e in match_xpaths(parent_node, [sub_xpath]):
      value = get_stripped_text_content(e)
      if value:
        value = strip_whitespace(value).strip()
      if value:
        sub_annotations.append(TargetAnnotation(value, sub_tag))
  return sub_annotations

def xml_root_to_target_annotations(xml_root, xml_mapping):
  if not xml_root.tag in xml_mapping:
    raise Exception("unrecognised tag: {} (available: {})".format(
      xml_root.tag, xml_mapping.sections())
    )

  mapping = xml_mapping[xml_root.tag]

  field_names = [k for k in mapping.keys() if '.' not in k]
  get_mapping_flag = lambda k, suffix: mapping.get(k + suffix) == 'true'
  get_match_multiple = lambda k: get_mapping_flag(k, XmlMappingSuffix.MATCH_MULTIPLE)
  get_bonding_flag = lambda k: get_mapping_flag(k, XmlMappingSuffix.BONDING)
  get_require_next_flag = lambda k: get_mapping_flag(k, XmlMappingSuffix.REQUIRE_NEXT)
  get_unmatched_parent_text_flag = (
    lambda k: get_mapping_flag(k, XmlMappingSuffix.UNMATCHED_PARENT_TEXT)
  )

  get_logger().debug('fields: %s', field_names)

  target_annotations_with_pos = []
  xml_pos_by_node = {node: i for i, node in enumerate(xml_root.iter())}
  for k in field_names:
    match_multiple = get_match_multiple(k)
    bonding = get_bonding_flag(k)
    require_next = get_require_next_flag(k)
    unmatched_parent_text = get_unmatched_parent_text_flag(k)
    children_xpaths = parse_xpaths(mapping.get(k + XmlMappingSuffix.CHILDREN))
    children_concat = parse_json_with_default(
      mapping.get(k + XmlMappingSuffix.CHILDREN_CONCAT), []
    )
    children_range = parse_json_with_default(
      mapping.get(k + XmlMappingSuffix.CHILDREN_RANGE), []
    )
    re_pattern = mapping.get(k + XmlMappingSuffix.REGEX)
    re_compiled_pattern = re.compile(re_pattern) if re_pattern else None
    priority = int(mapping.get(k + XmlMappingSuffix.PRIORITY, '0'))
    sub_xpaths = get_sub_mapping(mapping, k)
    get_logger().debug('sub_xpaths (%s): %s', k, sub_xpaths)

    xpaths = parse_xpaths(mapping[k])
    get_logger().debug('xpaths(%s): %s', k, xpaths)
    for e in match_xpaths(xml_root, xpaths):
      e_pos = xml_pos_by_node.get(e)

      sub_annotations = extract_sub_annotations(e, sub_xpaths)
      get_logger().debug('sub_annotations (%s): %s', k, sub_annotations)

      if children_xpaths:
        text_content_list, standalone_values = extract_children(
          e, children_xpaths, children_concat, children_range, unmatched_parent_text
        )
      else:
        text_content_list = filter_truthy(strip_all([get_stripped_text_content(e)]))
        standalone_values = []
      if re_compiled_pattern:
        text_content_list = filter_truthy([
          apply_pattern(s, re_compiled_pattern) for s in text_content_list
        ])
      if text_content_list:
        value = (
          text_content_list[0]
          if len(text_content_list) == 1
          else sorted(text_content_list, key=lambda s: -len(s))
        )
        target_annotations_with_pos.append((
          (-priority, e_pos),
          TargetAnnotation(
            value,
            k,
            match_multiple=match_multiple,
            bonding=bonding,
            require_next=require_next,
            sub_annotations=sub_annotations
          )
        ))
      if standalone_values:
        for i, standalone_value in enumerate(standalone_values):
          target_annotations_with_pos.append((
            (-priority, e_pos, i),
            TargetAnnotation(
              standalone_value,
              k,
              match_multiple=match_multiple,
              bonding=bonding,
              sub_annotations=sub_annotations
            )
          ))
  target_annotations_with_pos = sorted(
    target_annotations_with_pos,
    key=lambda x: x[0]
  )
  get_logger().debug('target_annotations_with_pos:\n%s', target_annotations_with_pos)
  target_annotations = [
    x[1] for x in target_annotations_with_pos
  ]
  get_logger().debug('target_annotations:\n%s', '\n'.join([
    ' ' + str(a) for a in target_annotations
  ]))
  return target_annotations
