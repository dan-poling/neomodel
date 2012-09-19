from py2neo import neo4j
from .indexbatch import IndexBatch
from lucenequerybuilder import Q
import types
import sys
import os


OUTGOING = neo4j.Direction.OUTGOING
INCOMING = neo4j.Direction.INCOMING
EITHER = neo4j.Direction.EITHER


class NeoDB(object):
    """ Manage and cache connections to neo4j """

    def __init__(self, graph_db):
        self.client = graph_db
        self.category_cache = {}

    def category(self, name):
        """ Retrieve category node by name """
        category_index = self.client.get_or_create_index(neo4j.Node, 'Category')

        if name not in self.category_cache:
            category = category_index.get_or_create('category', name, {'category': name})
            self.category_cache[name] = category

        return self.category_cache[name]


def connection_adapter():
    try:
        return connection_adapter.db
    except AttributeError:
        graph_db = neo4j.GraphDatabaseService(os.environ.get('NEO4J_URL'))
        connection_adapter.db = NeoDB(graph_db)
        return connection_adapter.db


class RelationshipInstaller(object):
    """Replace relationship definitions with instances of RelationshipManager"""

    def __init__(self, *args, **kwargs):
        self._related = {}

        for key, value in self.__class__.__dict__.iteritems():
            if value.__class__ == RelationshipDefinition\
                    or issubclass(value.__class__, RelationshipDefinition):
                self._setup_relationship(key, value)

    def _setup_relationship(self, rel_name, rel_object):
        self.__dict__[rel_name] = rel_object.build_manager(self, rel_name)


class RelationshipManager(object):
    def __init__(self, direction, relation_type, name, node_class, origin):
        self.direction = direction
        self.relation_type = relation_type
        self.node_class = node_class
        self.name = name
        self.related = {}
        self.origin = origin

    @property
    def client(self):
        return self.origin._db.client

    def all(self):
        if not self.related:
            related_nodes = self.origin._node.get_related_nodes(self.direction, self.relation_type)
            if not related_nodes:
                return
            for n in related_nodes:
                wrapped_node = self.node_class(**(n.get_properties()))
                wrapped_node._node = n
                self.related[n.id] = wrapped_node
            return [v for v in self.related.itervalues()]
        else:
            return [v for v in self.related.itervalues()]

    def is_related(self, obj):
        if obj._node.id in self.related:
            return True
        return self.origin._node.has_relationship_with(obj._node, self.direction, self.relation_type)

    def relate(self, obj):
        if obj.__class__ != self.node_class:
            raise Exception("Expecting object of class " + self.node_class.__name__)
        if not obj._node:
            raise Exception("Can't create relationship to unsaved node")

        self.client.get_or_create_relationships((self.origin._node, self.relation_type, obj._node),)
        self.related[obj._node.id] = obj

    def unrelate(self, obj):
        if obj._node.id in self.related:
            del self.related[obj._node.id]
        rels = self.origin._node.get_relationships_with(obj._node, self.direction, self.relation_type)
        if not rels:
            return
        if len(rels) > 1:
            raise Exception("Expected single relationship got {0}".format(rels))
        rels[0].delete()


class NeoIndex(object):
    def __init__(self, node_class, index):
        self.node_class = node_class
        self._index = index

    def search(self, query=None, **kwargs):
        """ Load multiple nodes via index """
        for k, v in kwargs.iteritems():
            p = self.node_class.get_property(k)
            if not p:
                raise NoSuchProperty(k)
            if not p.is_indexed:
                raise PropertyNotIndexed(k)
            p.validate(v)

        if not query:
            query = reduce(lambda x, y: x & y, [Q(k, v) for k, v in kwargs.iteritems()])

        result = self._index.query(str(query))
        nodes = []

        for node in result:
            properties = node.get_properties()
            neonode = self.node_class(**properties)
            neonode._node = node
            nodes.append(neonode)
        return nodes

    def get(self, query=None, **kwargs):
        """ Load single node via index """
        nodes = self.search(query, **kwargs)
        if len(nodes) == 1:
            return nodes[0]
        elif len(nodes) > 1:
            raise Exception("Multiple nodes returned from query, expected one")
        else:
            raise Exception("No nodes found")


class RelationshipDefinition(object):
    def __init__(self, relation_type, cls, direction, manager=RelationshipManager):
        self.relation_type = relation_type
        self.node_class = cls
        self.manager = manager
        self.direction = direction

    def build_manager(self, origin, name):
        return self.manager(
                self.direction,
                self.relation_type,
                name,
                self.node_class,
                origin
               )


class NeoNodeMeta(type):
    def __new__(cls, name, bases, dct):
        cls = super(NeoNodeMeta, cls).__new__(cls, name, bases, dct)
        if cls.__name__ != 'NeoNode':
            db = connection_adapter()
            index = db.client.get_or_create_index(neo4j.Node, name)
            cls.index = NeoIndex(cls, index)
        return cls


class NeoNode(RelationshipInstaller):
    """ Base class for nodes requiring formal declaration """

    __metaclass__ = NeoNodeMeta

    @classmethod
    def get_property(cls, name):
        node_property = getattr(cls, name)
        if not node_property or not issubclass(node_property.__class__, Property):
            Exception(name + " is not a Property of " + cls.__name__)
        return node_property

    @classmethod
    def relate(cls, manager_property, relation, to=None):
        rel_type, direction = relation
        if hasattr(cls, manager_property):
            raise Exception(cls.__name__ + " already has attribute " + manager_property)
        relationship = RelationshipDefinition(rel_type, to, direction)
        setattr(cls, manager_property, relationship)

    def __init__(self, *args, **kwargs):
        self._validate_args(kwargs)
        self._node = None
        self._db = connection_adapter()
        self._type = self.__class__.__name__
        self._index = self._db.client.get_or_create_index(neo4j.Node, self._type)

        super(NeoNode, self).__init__(*args, **kwargs)

    def __setattr__(self, key, value):
        if key.startswith('_'):
            self.__dict__[key] = value
            return
        prop = self.__class__.get_property(key)
        if prop.validate(value):
            self.__dict__[key] = value

    @property
    def properties(self):
        """ Return properties and values of a node """
        props = {}
        # exclude methods and anything prefixed '_'
        for key, value in self.__dict__.iteritems():
            if not key.startswith('_'):
                if not isinstance(value, types.MethodType):
                    if not isinstance(value, RelationshipManager):
                        props[key] = value
        return props

    def _validate_args(self, props):
        """ Validate dict and set node properties """
        for key, value in props.iteritems():
            if key in self.__class__.__dict__:
                node_property = self.__class__.get_property(key)
                node_property.validate(value)
                self.__dict__[key] = value
            else:
                raise NoSuchProperty(key)

    def _create(self, props):
        # TODO make this single atomic operation
        relation_name = self._type.upper()
        self._node, rel = self._db.client.create(props,
                (self._db.category(self._type), relation_name, 0))
        if not self._node:
            Exception('Failed to create new ' + self._type)

        # Update indexes
        try:
            self._update_index(props)
        except Exception:
            exc_info = sys.exc_info()
            self.delete()
            raise exc_info[1], None, exc_info[2]

    def _update_index(self, props):
        batch = IndexBatch(self._index)
        for key, value in props.iteritems():
            node_property = self.__class__.get_property(key)
            if node_property.unique_index:
                batch.add_if_none(key, value, self._node)
            elif node_property.index:
                batch.add(key, value, self._node)
        if 200 in [r.status for r in batch.submit()]:
            raise NotUnique('A supplied value is not unique' + r.uri)

    def save(self):
        if self._node:
            self._node.set_properties(self.properties)
            self._index.remove(entity=self._node)
            self._update_index(self.properties)
        else:
            self._create(self.properties)
        return self

    def delete(self):
        if self._node:
            to_delete = self._node.get_relationships()
            to_delete.append(self._node)
            self._db.client.delete(*to_delete)
            self._node = None
        else:
            raise Exception("Node has not been saved so cannot be deleted")
        return True


# TODO handle 'blank' correctly
class Property(object):
    def __init__(self, unique_index=False, index=False, blank=False):
        if unique_index and index:
            raise Exception("unique_index and index are mutually exclusive")
        if unique_index and blank:
            raise Exception("uniquely indexed properties cannot also be blank")
        self.unique_index = unique_index
        self.index = index

    @property
    def is_indexed(self):
        return self.unique_index or self.index


class StringProperty(Property):
    def validate(self, value):
        if isinstance(value, (str, unicode)):
            return True
        else:
            raise TypeError("Object of type str expected got " + str(value))


class IntegerProperty(Property):
    def validate(self, value):
        if isinstance(value, (int, long)):
            return True
        else:
            raise TypeError("Object of type int or long expected")


class NoSuchProperty(Exception):
    pass


class PropertyNotIndexed(Exception):
    pass


class NotUnique(Exception):
    pass
