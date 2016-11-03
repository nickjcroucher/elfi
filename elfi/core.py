import numpy as np
import uuid

import operator

from dask.delayed import delayed, Delayed
import itertools
from functools import partial
from .utils import to_slice, slice_intersect, slen
from . import env

DEFAULT_DATATYPE = np.float32


class Node(object):
    """
    Attributes
    ----------
    values : numpy array or None
             stores generated values
    """
    def __init__(self, name, *parents):
        self.name = name
        self.parents = []
        self.children = set()
        for p in list(parents):
            self.add_parent(p)

    def add_parents(self, nodes):
        for n in self.node_list(nodes):
            self.add_parent(n)

    def add_parent(self, node, index=None):
        node = self.ensure_node(node)
        if index is None:
            index = len(self.parents)
        self.parents.insert(index, node)
        node.children.add(self)

    def add_children(self, nodes):
        for n in set(self.node_list(nodes)):
            self.add_child(n)

    def add_child(self, node):
        node = self.ensure_node(node)
        node.add_parent(self)

    def is_root(self):
        return len(self.parents) == 0

    def is_leaf(self):
        return len(self.children) == 0

    def remove(self, keep_parents=False, keep_children=False):
        if not keep_parents:
            for i in range(len(self.parents)):
                self.remove_parent(0)
        if not keep_children:
            for c in self.children.copy():
                c.remove_parent(self)

    def remove_parent(self, parent_or_index=None):
        index = parent_or_index
        if isinstance(index, Node):
            for i, p in enumerate(self.parents):
                if p == parent_or_index:
                    index = i
                    break
        if isinstance(index, Node):
            raise Exception("Could not find a parent")
        parent = self.parents[index]
        del self.parents[index]
        parent.children.remove(self)
        return index

    def replace_by(self, node, transfer_parents=True, transfer_children=True):
        """

        Parameters
        ----------
        node : Node
        transfer_parents
        transfer_children

        Returns
        -------

        """
        if transfer_parents:
            parents = self.parents.copy()
            for p in parents:
                self.remove_parent(p)
            node.add_parents(parents)

        if transfer_children:
            children = self.children.copy()
            for c in children:
                index = c.remove_parent(self)
                c.add_parent(node, index=index)

    @property
    def component(self):
        """Depth first search"""
        c = {}
        search = [self]
        while len(search) > 0:
            current = search.pop()
            if current.name in c:
                continue
            c[current.name] = current
            search += list(current.neighbours)
        return list(c.values())

    #@property
    #def graph(self):
    #    return Graph(self)

    @property
    def label(self):
        return self.name

    @property
    def neighbours(self):
        n = set(self.children)
        n = n.union(self.parents)
        return list(n)

    """Private methods"""

    def convert_to_node(self, obj, name):
        raise ValueError("No conversion to Node for value {}".format(obj))

    def ensure_node(self, obj):
        if isinstance(obj, Node):
            return obj
        name = "_{}_{}".format(self.name, str(uuid.uuid4().hex[0:6]))
        return self.convert_to_node(obj, name)

    """Static methods"""

    @staticmethod
    def node_list(nodes):
        if isinstance(nodes, dict):
            nodes = nodes.values()
        elif isinstance(nodes, Node):
            nodes = [nodes]
        return nodes


# TODO: add version number to key so that resets are not confused in dask scheduler
def make_key(name, sl):
    """Makes the dask key for the outputs of nodes

    Parameters
    ----------
    name : string
        name of the output (e.g. node name)
    sl : slice
        data slice that is covered by this output

    Returns
    -------
    a tuple key
    """
    n = slen(sl)
    if n <= 0:
        ValueError('Slice has no length')
    return (name, sl.start, n)


def get_key_slice(key):
    """Returns the corresponding slice from `key`"""
    return slice(key[1], key[1] + key[2])


def get_key_name(key):
    return key[0]


def reset_key_slice(key, new_sl):
    """Resets the slice from `key` to `new_sl`

    Returns
    -------
    a new key
    """
    return make_key(get_key_name(key), new_sl)


def reset_key_name(key, name):
    """Resets the name from `key` to `name`

    Returns
    -------
    a new key
    """
    return make_key(name, get_key_slice(key))


class DataStore:
    def save(self, output_data, sl):
        """Returns a `dask.delayed` object that points to the stored data"""
        raise NotImplementedError

    def read(self, sl):
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError


class MemoryStore(DataStore):
    """Keeps results in memory of the workers"""
    def __init__(self):
        self._futures = {}

    def save(self, output_data, sl):
        # Send to be computed
        f = env.client().compute(output_data)
        self._futures[self._sl_key(sl)] = f
        return delayed(True)

    def read(self, sl):
        # TODO: allow arbitrary slices to be taken, now they have to match
        f = self._futures[self._sl_key(sl)]
        # TODO: Some more informative constant in place of `'result cached'`
        return Delayed(f.key, {f.key: 'result cached'})

    def reset(self):
        for f in self._futures.values():
            del f
        self._futures = {}

    def _sl_key(self, sl):
        return sl.start, sl.stop


class OutputHandler:
    """Handles a continuous list of outputs for a node

    """
    def __init__(self, store=None):
        self._delayed_outputs = []
        self._data_store = store

    def __len__(self):
        len = 0
        for o in self._delayed_outputs:
            len += o.key[2]
        return len

    def append(self, output):
        """Appends output to cache/store

        """
        if len(self) != get_key_slice(output.key).start:
            raise ValueError('Appending a non matching slice')

        if self._data_store:
            sl = get_key_slice(output.key)
            output_data = self.get_named_item(output, 'data')
            stored = self._data_store.save(output_data, sl)
            stored.add_done_callback(lambda f: self._stored(f, output.key))

        self._delayed_outputs.append(output)

    def reset(self):
        # TODO: reset self also
        if self._data_store:
            self._data_store.reset()

    def __getitem__(self, sl):
        """
        Returns the data in slice `sl`
        """
        sl = to_slice(sl)
        outputs = self._get_output_datalist(sl)

        # Return the data_slice
        if len(outputs) == 0:
            empty = np.atleast_2d([])
            output = delayed(empty)
        elif len(outputs) == 1:
            output = outputs[0]
        else:
            key = reset_key_slice(outputs[0].key, sl)
            output = delayed(np.vstack)(tuple(outputs), dask_key_name=key)

        return output

    def _get_output_datalist(self, sl):
        outputs = []
        for output in self._delayed_outputs:
            output_sl = get_key_slice(output.key)
            intsect_sl = slice_intersect(output_sl, sl)
            if slen(intsect_sl) == 0:
                continue
            output = self.__class__.get_named_item(output, 'data')
            if slen(intsect_sl) != slen(output_sl):
                # Take a subset of the data-slice
                intsect_key = reset_key_slice(output.key, intsect_sl)
                sub_sl = slice_intersect(intsect_sl, offset=output_sl.start)
                output = delayed(operator.getitem)(output, sub_sl, dask_key_name=intsect_key)
            outputs.append(output)
        return outputs

    def _stored(self, future, key):
        # TODO: test if future was successful
        output = [i for i,o in enumerate(self._delayed_outputs) if o.key == key]
        if len(output) != 1:
            raise LookupError('Cannot find output with the given key')
        idx = output[0]
        # Replace the delayed with the one from store
        self._delayed_outputs[idx] = self._data_store.read(get_key_slice(key))
        del future


    @staticmethod
    def get_named_item(output, item):
        new_key_name = get_key_name(output.key) + '-' + str(item)
        new_key = reset_key_name(output.key, new_key_name)
        return delayed(operator.getitem)(output, item, dask_key_name=new_key)


def to_output(input, **kwargs):
    output = input.copy()
    for k, v in kwargs.items():
        output[k] = v
    return output


substreams = itertools.count()


def normalize_data(data, n):
    """Broadcasts scalars and lists to 2d numpy arrays with distinct values along axis 0.
    Normalization rules:
    - Scalars will be broadcasted to (n,1) arrays
    - One dimensional arrays with length l will be broadcasted to (l,1) arrays
    - Over one dimensional arrays of size (1, ...) will be broadcasted to (n, ...) arrays
    """
    if data is None:
        return None
    data = np.atleast_1d(data)
    # Handle scalars and 1 dimensional arrays
    if data.ndim == 1:
        data = data[:, None]
    # Here we have at least 2d arrays
    if len(data) == 1:
        data = np.tile(data, (n,) + (1,) * (data.ndim - 1))
    return data


def normalize_data_dict(dict, n):
    if dict is None:
        return None
    normalized = {}
    for k, v in dict.items():
        normalized[k] = normalize_data(v, n)
    return normalized


class Operation(Node):
    def __init__(self, name, operation, *parents, store=None):
        """

        Parameters
        ----------
        name : name of the node
        operation : node operation function
        *parents : parents of the nodes
        store : `DataStore` instance
        """
        super(Operation, self).__init__(name, *parents)
        self.operation = operation

        self._generate_index = 0
        self._store = OutputHandler(store)
        self.reset(propagate=False)

    def acquire(self, n, starting=0, batch_size=None):
        """
        Acquires values from the start or from starting index.
        Generates new ones if needed.
        """
        sl = slice(starting, starting+n)
        if self._generate_index < sl.stop:
            self.generate(sl.stop - self._generate_index, batch_size=batch_size)
        return self.get_slice(sl)

    def generate(self, n, batch_size=None, with_values=None):
        """
        Generate n new values from the node
        """
        a = self._generate_index
        b = a + n
        batch_size = batch_size or n
        with_values = normalize_data_dict(with_values, n)

        # TODO: with_values cannot be used with already generated values
        # Ensure store is filled up to `b`
        while len(self._store) < b:
            l = len(self._store)
            n_batch = min(b-l, batch_size)
            batch_sl = slice(l, l+n_batch)
            batch_values = None
            if with_values is not None:
                batch_values = {k: v[(l-a):(l-a)+n_batch] for k,v in with_values.items()}
            self.get_slice(batch_sl, with_values=batch_values)

        self._generate_index = b
        return self[slice(a, b)]

    def __getitem__(self, sl):
        sl = to_slice(sl)
        return self._store[sl]

    def get_slice(self, sl, with_values=None):
        """
        This function is ensured to give a slice anywhere (already generated or not)
        """
        # TODO: prevent using with_values with already generated values
        # Check if we need to generate new
        if len(self._store) < sl.stop:
            with_values = normalize_data_dict(with_values, sl.stop - len(self._store))
            new_sl = slice(len(self._store), sl.stop)
            new_input = self._create_input_dict(new_sl, with_values=with_values)
            new_output = self._create_output(new_sl, new_input, with_values)
            self._store.append(new_output)
        return self[sl]

    def reset(self, propagate=True):
        """Resets the data of the node

        Resets the node to a state as if no data was generated from it.
        If propagate is True (default) also resets its descendants

        Parameters
        ----------
        propagate : bool

        """
        if propagate:
            for c in self.children:
                c.reset()
        self._generate_index = 0
        self._store.reset()

    def _create_input_dict(self, sl, with_values=None):
        n = sl.stop - sl.start
        input_data = tuple([p.get_slice(sl, with_values) for p in self.parents])
        return {
            'data': input_data,
            'n': n,
            'index': sl.start,
        }

    def _create_output(self, sl, input_dict, with_values=None):
        """

        Parameters
        ----------
        sl : slice
        input_dict : dict
        with_values : numpy.array

        Returns
        -------
        out : dask.delayed object
            object.key is (self.name, sl.start, n)

        """
        with_values = with_values or {}
        dask_key_name = make_key(self.name, sl)
        if self.name in with_values:
            # Set the data to with_values
            output = to_output(input_dict, data=with_values[self.name])
            return delayed(output, name=dask_key_name)
        else:
            dinput = delayed(input_dict, pure=True)
            return delayed(self.operation)(dinput,
                                           dask_key_name=dask_key_name)

    def convert_to_node(self, obj, name):
        return Constant(name, obj)


class Constant(Operation):
    def __init__(self, name, value):
        self.value = np.array(value, ndmin=1)
        v = self.value.copy()
        super(Constant, self).__init__(name, lambda input_dict: {'data': v})


"""
Operation mixins add additional functionality to the Operation class.
They do not define the actual operation. They only add keyword arguments.
"""


def get_substream_state(master_seed, substream_index):
    """Returns PRNG internal state for the sub stream

    Parameters
    ----------
    master_seed : uint32
    substream_index : uint

    Returns
    -------
    out : tuple
    Random state for the sub stream as defined by numpy

    See Also
    --------
    `numpy.random.RandomState.get_state` for the representation of MT19937 state

    """
    # Fixme: In the future, allow MRG32K3a from https://pypi.python.org/pypi/randomstate
    seeds = np.random.RandomState(master_seed)\
        .randint(np.iinfo(np.uint32).max, size=substream_index+1)
    return np.random.RandomState(seeds[substream_index]).get_state()


class RandomStateMixin(Operation):
    """
    Makes Operation node stochastic
    """
    def __init__(self, *args, **kwargs):
        super(RandomStateMixin, self).__init__(*args, **kwargs)
        # Fixme: decide where to set the inference model seed
        self.seed = 0

    def _create_input_dict(self, sl, **kwargs):
        dct = super(RandomStateMixin, self)._create_input_dict(sl, **kwargs)
        dct['random_state'] = self._get_random_state()
        return dct

    def _get_random_state(self):
        i_subs = next(substreams)
        return delayed(get_substream_state, pure=True)(self.seed, i_subs)


class ObservedMixin(Operation):
    """
    Adds observed data to the class
    """

    def __init__(self, *args, observed=None, **kwargs):
        super(ObservedMixin, self).__init__(*args, **kwargs)
        if observed is None:
            observed = self._inherit_observed()
        self.observed = np.array(observed, ndmin=2)

    def _inherit_observed(self):
        if len(self.parents) and hasattr(self.parents[0], 'observed'):
            observed = tuple([p.observed for p in self.parents])
            observed = self.operation({'data': observed})['data']
        else:
            raise ValueError('There is no observed value to inherit')
        return observed



"""
ABC specific Operation nodes
"""


# For python simulators using numpy random variables
def simulator_operation(simulator, vectorized, input_dict):
    """ Calls the simulator to produce output

    Vectorized simulators
    ---------------------
    Calls the simulator(*vectorized_args, n_sim, prng) to create output.
    Each vectorized argument to simulator is a numpy array with shape[0] == 'n_sim'.
    Simulator should return a numpy array with shape[0] == 'n_sim'.

    Sequential simulators
    ---------------------
    Calls the simulator(*args, prng) 'n_sim' times to create output.
    Each argument to simulator is of the dtype of the original array[i].
    Simulator should return a numpy array.

    Parameters
    ----------
    simulator: function
    vectorized: bool
    input_dict: dict
        "n": number of parallel simulations
        "data": list of args as numpy arrays
    """
    # set the random state
    prng = np.random.RandomState(0)
    prng.set_state(input_dict['random_state'])
    n_sim = input_dict['n']
    if vectorized is True:
        data = simulator(*input_dict['data'], n_sim=n_sim, prng=prng)
    else:
        data = None
        for i in range(n_sim):
            inputs = [v[i] for v in input_dict["data"]]
            d = simulator(*inputs, prng=prng)
            if data is None:
                data = np.zeros((n_sim,) + d.shape)
            data[i,:] = d
    return to_output(input_dict, data=data, random_state=prng.get_state())


class Simulator(ObservedMixin, RandomStateMixin, Operation):
    """ Simulator node

    Parameters
    ----------
    name: string
    simulator: function
    vectorized: bool
        whether the simulator function is vectorized or not
        see definition of simulator_operation for more information
    """
    def __init__(self, name, simulator, *args, vectorized=True, **kwargs):
        operation = partial(simulator_operation, simulator, vectorized)
        super(Simulator, self).__init__(name, operation, *args, **kwargs)


def summary_operation(operation, input):
    data = operation(*input['data'])
    return to_output(input, data=data)


class Summary(ObservedMixin, Operation):
    def __init__(self, name, operation, *args, **kwargs):
        operation = partial(summary_operation, operation)
        super(Summary, self).__init__(name, operation, *args, **kwargs)


def discrepancy_operation(operation, input):
    data = operation(input['data'], input['observed'])
    return to_output(input, data=data)


class Discrepancy(Operation):
    """
    The operation input has a tuple of data and tuple of observed
    """
    def __init__(self, name, operation, *args, **kwargs):
        operation = partial(discrepancy_operation, operation)
        super(Discrepancy, self).__init__(name, operation, *args, **kwargs)

    def _create_input_dict(self, sl, **kwargs):
        dct = super(Discrepancy, self)._create_input_dict(sl, **kwargs)
        dct['observed'] = observed = tuple([p.observed for p in self.parents])
        return dct


def threshold_operation(threshold, input):
    data = input['data'][0] < threshold
    return to_output(input, data=data)


class Threshold(Operation):
    def __init__(self, name, threshold, *args, **kwargs):
        operation = partial(threshold_operation, threshold)
        super(Threshold, self).__init__(name, operation, *args, **kwargs)


"""
Other functions
"""


def fixed_expand(n, fixed_value):
    """
    Creates a new axis 0 (or dimension) along which the value is repeated
    """
    return np.repeat(fixed_value[np.newaxis,:], n, axis=0)







# class Graph(object):
#     """A container for the graphical model"""
#     def __init__(self, anchor_node=None):
#         self.anchor_node = anchor_node
#
#     @property
#     def nodes(self):
#         return self.anchor_node.component
#
#     def sample(self, n, parameters=None, threshold=None, observe=None):
#         raise NotImplementedError
#
#     def posterior(self, N):
#         raise NotImplementedError
#
#     def reset(self):
#         data_nodes = self.find_nodes(Data)
#         for n in data_nodes:
#             n.reset()
#
#     def find_nodes(self, node_class=Node):
#         nodes = []
#         for n in self.nodes:
#             if isinstance(n, node_class):
#                 nodes.append(n)
#         return nodes
#
#     def __getitem__(self, key):
#         for n in self.nodes:
#             if n.name == key:
#                 return n
#         raise IndexError
#
#     def __getattr__(self, item):
#         for n in self.nodes:
#             if n.name == item:
#                 return n
#         raise AttributeError
#
#     def plot(self, graph_name=None, filename=None, label=None):
#         from graphviz import Digraph
#         G = Digraph(graph_name, filename=filename)
#
#         observed = {'shape': 'box', 'fillcolor': 'grey', 'style': 'filled'}
#
#         # add nodes
#         for n in self.nodes:
#             if isinstance(n, Fixed):
#                 G.node(n.name, xlabel=n.label, shape='point')
#             elif hasattr(n, "observed") and n.observed is not None:
#                 G.node(n.name, label=n.label, **observed)
#             # elif isinstance(n, Discrepancy) or isinstance(n, Threshold):
#             #     G.node(n.name, label=n.label, **observed)
#             else:
#                 G.node(n.name, label=n.label, shape='doublecircle',
#                        fillcolor='deepskyblue3',
#                        style='filled')
#
#         # add edges
#         edges = []
#         for n in self.nodes:
#             for c in n.children:
#                 if (n.name, c.name) not in edges:
#                     edges.append((n.name, c.name))
#                     G.edge(n.name, c.name)
#             for p in n.parents:
#                 if (p.name, n.name) not in edges:
#                     edges.append((p.name, n.name))
#                     G.edge(p.name, n.name)
#
#         if label is not None:
#             G.body.append("label=" + '\"' + label + '\"')
#
#         return G
#
#     """Properties"""
#
#     @property
#     def thresholds(self):
#         return self.find_nodes(node_class=Threshold)
#
#     @property
#     def discrepancies(self):
#         return self.find_nodes(node_class=Discrepancy)
#
#     @property
#     def simulators(self):
#         return [node for node in self.nodes if isinstance(node, Simulator)]
#
#     @property
#     def priors(self):
#         raise NotImplementedError
#         #Implementation wrong, prior have Value nodes as hyperparameters
#         # priors = self.find_nodes(node_class=Stochastic)
#         # priors = {n for n in priors if n.is_root()}
#         # return priors
