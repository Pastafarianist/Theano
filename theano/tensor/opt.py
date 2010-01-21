"""Tensor optimizations addressing the ops in basic.py
"""
# TODO: intelligent merge for mul/add
# TODO: 0*x -> 0

import logging
_logger = logging.getLogger('theano.tensor.opt')

from theano import gof
from theano.gof import opt, InconsistencyError, TopoOptimizer, graph
from theano.gof.utils import MethodNotDefined
import theano.config as config
from elemwise import Elemwise, DimShuffle
from theano import scalar
import basic as T
import inplace as I
import numpy
import numpy as N #guys... please don't do this in the library :(
import operator
import itertools
import sys, os
from theano import compile  #to register the optimizer built by this file

from theano.gof.python25 import any, all
from theano.gof.opt import Optimizer
from theano.gof import toolbox, DestroyHandler
# Utilities

def out2in(*local_opts):
    """WRITEME """
    return opt.TopoOptimizer(opt.LocalOptGroup(*local_opts),
                             order = 'out_to_in',
                             failure_callback=TopoOptimizer.warn_inplace)

def in2out(*local_opts, **kwargs):
    """WRITEME """
    return opt.TopoOptimizer(opt.LocalOptGroup(*local_opts),
                             order = 'in_to_out',
                             failure_callback=TopoOptimizer.warn_inplace,
                             **kwargs)

def _fill_chain(new_out, orig_inputs):
    for i in orig_inputs:
        new_out = T.fill(i, new_out)
    return [new_out]

def get_constant_value(v, fill=False):
    """return the constant value underlying variable `v`

    If v is the output of dimshuffles, fills, this function digs through them.

    If `v` is not some view of constant data, then raise a TypeError.

    if fill is True, then it returns (v, [...]) where the second term is a list of variables
    that were used in the fill expressions

    :note: There may be another function similar to this one in the code, but I'm not sure where it
    is.
    """

    if isinstance(v, gof.Constant):
        if fill:
            return v.data, []
        return v.data
    if v.owner and isinstance(v.owner.op, T.DimShuffle):
        return get_constant_value(v.owner.inputs[0], fill=fill)
    if fill:
        if v.owner and v.owner.op == T.fill:
            shape, val = v.owner.inputs
            # fill(a,b) fills the shape of 'a' filled with 'b'
            rval, rshapes = get_constant_value(val, fill=fill)
            return rval, rshapes + [shape]
    raise TypeError(v)

@gof.optimizer
def insert_inplace_optimizer(env):
    """
    Usage: inplace_optimizer.optimize(env)
    
    Attempts to replace all Broadcast ops by versions of them
    that operate inplace. It operates greedily: for each Broadcast
    Op that is encountered, for each output, tries each input to
    see if it can operate inplace on that input. If so, makes the
    change and go to the next output or Broadcast Op.

    Examples:
      x + y + z -> x += y += z
      (x + y) * (x * y) -> (x += y) *= (x * y) or (x + y) *= (x *= y)
    """
    for node in list(graph.io_toposort(env.inputs, env.outputs)):
        op = node.op
        if not isinstance(op, Elemwise):
            continue
        baseline = op.inplace_pattern
        candidate_outputs = [i for i in xrange(len(node.outputs)) if i not in baseline]
        candidate_inputs = [i for i in xrange(len(node.inputs)) if i not in baseline.values()]
        for candidate_output in candidate_outputs:
            for candidate_input in candidate_inputs:
                inplace_pattern = dict(baseline, **{candidate_output: candidate_input})
                try:
                    new = Elemwise(
                        op.scalar_op.__class__(
                            scalar.transfer_type(
                                *[inplace_pattern.get(i, None) \
                                        for i in xrange(len(node.outputs))])),
                        inplace_pattern).make_node(*node.inputs)
                    env.replace_all_validate(zip(node.outputs, new.outputs),
                            reason="insert_inplace_optimizer")
                except (ValueError, TypeError, InconsistencyError), e:
                    continue
                candidate_inputs.remove(candidate_input)
                node = new
                baseline = inplace_pattern
                break
compile.optdb.register('inplace_opt', insert_inplace_optimizer, 75, 'fast_run', 'inplace') 

def register_canonicalize(lopt, *tags, **kwargs):
    name = (kwargs and kwargs.pop('name')) or lopt.__name__
    compile.optdb['canonicalize'].register(name, lopt, 'fast_run', *tags)
    return lopt

def register_specialize(lopt, *tags, **kwargs):
    name = (kwargs and kwargs.pop('name')) or lopt.__name__
    compile.optdb['specialize'].register(name, lopt, 'fast_run', *tags)
    return lopt

######################
# DimShuffle lifters #
######################

@gof.local_optimizer([None, None])
def local_dimshuffle_lift(node):
    """
    "Lifts" DimShuffle through Elemwise operations and merges
    consecutive DimShuffles. Basically, applies the following
    transformations on the whole graph:

    DimShuffle(Elemwise(x, y)) => Elemwise(DimShuffle(x), DimShuffle(y))
    DimShuffle(DimShuffle(x)) => DimShuffle(x)

    After this transform, clusters of Elemwise operations are
    void of DimShuffle operations.
    """
    op = node.op
    if not isinstance(op, DimShuffle):
        return False

    input = node.inputs[0]
    inode = input.owner
    if inode and isinstance(inode.op, Elemwise) and (len(input.clients)==1):
        return inode.op.make_node(*[DimShuffle(input.type.broadcastable,
                                               op.new_order,
                                               op.inplace)(input) for input in inode.inputs]).outputs
    if inode and isinstance(inode.op, DimShuffle):
        new_order = [x == 'x' and 'x' or inode.op.new_order[x] for x in op.new_order]
        inplace = op.inplace and inode.op.inplace
        iinput = inode.inputs[0]
        if new_order == range(len(new_order)):
            return [iinput]
        else:
            return DimShuffle(iinput.type.broadcastable, new_order, inplace).make_node(iinput).outputs

register_canonicalize(local_dimshuffle_lift)



#################
# Shape lifters #
#################

@gof.local_optimizer([T._shape, None])
def local_shape_lift_elemwise(node):
    """
    shape(elemwise_op(..., x, ...)) -> shape(x)

    Where x contains the maximal shape information.
    """
    if not opt.check_chain(node, T._shape, T.Elemwise):
        return False

    output = node.inputs[0]
    parent = output.owner

    for input in parent.inputs:
        if input.type.broadcastable == output.type.broadcastable:
            return T._shape(input),

    return False

register_canonicalize(local_shape_lift_elemwise, 'shape_lift')
register_specialize(local_shape_lift_elemwise, 'shape_lift')


@gof.local_optimizer([T._shape, None])
def local_shape_lift_sum(node):
    """
    shape(sum{n}(x)) -> [shape(x)[0], ..., shape(x)[n-1], shape(x)[n+1], ...]
    """
    if not opt.check_chain(node, T._shape, T.Sum):
        return False

    input = node.inputs[0].owner.inputs[0]
    axis = node.inputs[0].owner.op.axis
    if axis is None:# or len(axis) != 1:
        axis = range(input.type.ndim)


    ish = T._shape(input)
    return T.make_lvector.make_node(*(ish[i] for i in xrange(input.type.ndim) if i not in axis)).outputs
#    return T.vertical_stack.make_node(ish[:axis], ish[axis+1:]).outputs

register_canonicalize(local_shape_lift_sum, 'shape_lift')


@gof.local_optimizer([T._shape, T.dot])
def local_shape_lift_dot(node):
    """
    shape(dot(a, b)) -> [shape(a)[0], shape(b)[1]]
    """
    if not opt.check_chain(node, T._shape, T.dot):
        return False
    a, b = node.inputs[0].owner.inputs
    if a.type.ndim == 2 and b.type.ndim == 2:
        return T.make_lvector.make_node(T._shape(a)[0], T._shape(b)[1]).outputs
    elif a.type.ndim == 1 and b.type.ndim == 2:
        return T.make_lvector.make_node(T._shape(b)[1]).outputs
    elif a.type.ndim == 2 and b.type.ndim == 1:
        return T.make_lvector.make_node(T._shape(a)[0]).outputs
    elif a.type.ndim == 1 and b.type.ndim == 1:
        return T.make_lvector.make_node().outputs
    else:
        return False

register_canonicalize(local_shape_lift_dot, 'shape_lift')


# local_shape_lift = opt.LocalOptGroup(local_shape_lift_elemwise,
#                                      local_shape_lift_sum,
#                                      local_shape_lift_dot)


################
# Fill lifters #
################

def encompasses_broadcastable(b1, b2):
    """
    Returns True if the broadcastable patterns b1 and b2 are such that b2 is
    broadcasted to b1's shape and not the opposite.

    :param b1: the broadcastable attribute of a tensor type
    :param b2: the broadcastable attribute of a tensor type
    """
    if len(b1) < len(b2):
        return False
    b1 = b1[-len(b2):]
    return not any(v1 and not v2 for v1, v2 in zip(b1, b2))

def merge_broadcastables(broadcastables):
    return [all(bcast) for bcast in zip(*broadcastables)]

@gof.local_optimizer([T.fill, None])
def local_fill_lift(node):
    """
    fill(f(a), b) -> fill(a, b)
    If a.type == f(a).type.

    fill(a, b) -> b
    If a.type == b.type.
    """
    if not opt.check_chain(node, T.fill):
        return False

    model, filling = node.inputs

    mb, fb = model.type.broadcastable, filling.type.broadcastable
    if model.type.dtype == filling.type.dtype and encompasses_broadcastable(fb, mb):
        return False# [filling]

    parent = model.owner
    if parent is None or not isinstance(parent, T.Elemwise):
        return False
    for input in parent.inputs:
        if input.type == model.type:
            return [T.fill(input, filling)]

    return False

register_canonicalize(local_fill_lift, 'fill_lift')


##################
# Subtensor opts #
##################


@gof.local_optimizer([None, None])
def local_subtensor_make_vector(node):
    """
    [a,b,c][0] -> a
    [a,b,c][0:2] -> [a,b]

    If the index or slice is constant.
    """
    if not opt.check_chain(node, T.Subtensor, T.MakeVector):
        return False

    joined_r = node.inputs[0]

    try: 
        #check that join is being used to join scalars
        veclen = T.join.vec_length(joined_r)
    except:
        return False

    idxlist = node.op.idx_list
    if len(idxlist) != 1:
        return False
    idx = idxlist[0]
    if isinstance(idx, int):
        return [node.inputs[0].owner.inputs[idx]]
    try:
        return T.make_vector(*(node.owner.inputs[0].owner.inputs.__getslice__(idx)))
    except TypeError:
        return False

register_canonicalize(local_subtensor_make_vector)

@register_canonicalize
@gof.local_optimizer([None])
def local_IncSubtensor_serialize(node):
    """
    When using Subtensor, gradient graphs can be ugly.

    If we ask for grad(f(a[0]), a), we are going to get something like

        IncSubtensor(Elemwise{second}(a, 0), g(f(a[0])), [0])

    This might be ugly, but at least it's as fast as you could want.  If we ask for
    grad(f(a[0], a[1], a[2]), a), it's much worse...

        Elemwise{Add}
            IncSubtensor(Elemwise{second}(a, 0), g(f(a[0])), [0])
            IncSubtensor(Elemwise{second}(a, 0), g(f(a[1])), [1])
            IncSubtensor(Elemwise{second}(a, 0), g(f(a[2])), [2])

    This is much worse because this time we have to produce 3 matrices the size of 'a', just so
    we can add them together. 
    
    This Op rearranges IncSubtensor's that all work on the same initial argument (here,
    Elemwise{second}(a,0)) into a chain.  The advantage of the chain structure is that each one
    can be optimized later in the pipeline to operate inplace.

    Ideally, the op will do something like this:

    #
    #  add(x, incsubtensor(b, c), incsubtensor(b, d))
    #  -> incsubtensor(incsubtensor(add(x,b,b), c), d)
    
    """
    def movable(i):
        # Return True iff this is a incsubtensor that we can move
        return i.owner \
                and isinstance(i.owner.op, T.IncSubtensor) \
                and i.type == o_type \
                and len(i.clients) == 1 \
                and not i.owner.op.set_instead_of_inc

    if node.op == T.add:
        o_type = node.outputs[0].type

        movable_inputs = [i for i in node.inputs if movable(i)]

        if movable_inputs:
            new_inputs = [i for i in node.inputs if not movable(i)] \
                    + [mi.owner.inputs[0] for mi in movable_inputs]
            new_add = T.add(*new_inputs)

            # stack up the new incsubtensors
            tip = new_add
            for mi in movable_inputs:
                assert tip.type == o_type
                assert tip.type == mi.owner.inputs[0].type
                tip = mi.owner.op(tip, *mi.owner.inputs[1:])
            return [tip]

        #print incsub_inputs, [id(i.owner.inputs[0]) for i in incsub_inputs]


#after priority 50 Destructive inplace operations
#gemm is the first one now, at priority 70

@gof.local_optimizer([None])
def local_inplace_setsubtensor(node):
    if isinstance(node.op, T.IncSubtensor) and not node.op.inplace:
        new_op = T.IncSubtensor(node.op.idx_list, inplace=True, \
                        set_instead_of_inc=node.op.set_instead_of_inc)
        new_node = new_op(*node.inputs)
        return [new_node]
    return False
compile.optdb.register('inplace_setsubtensor', TopoOptimizer(local_inplace_setsubtensor,
    failure_callback=TopoOptimizer.warn_inplace), 60, 'fast_run', 'inplace') #DEBUG

##################
# Reshape opts   #
##################


@gof.local_optimizer([None, None])
def local_reshape_chain(node):
    """
    Reshape(Reshape(shape1),shape2) -> Reshape(shape2)
    """
    if not opt.check_chain(node, T.Reshape, T.Reshape):
        return False
    
    return [node.op(node.inputs[0].owner.inputs[0], node.inputs[1])]
register_canonicalize(local_reshape_chain)


##################
# Middleman cuts #
##################

@gof.local_optimizer([None, T.fill])
def local_fill_cut(node):
    """
    f(fill(a,b), c) -> f(b, c)
    If c.type == a.type.
    """

    if not opt.check_chain(node, T.Elemwise):
        return False
    
    output = node.outputs[0]
    try:
        reference = [input
                     for input in node.inputs
                     if input.type == output.type and (not input.owner or input.owner.op != T.fill)][0]
    except IndexError:
        return False

    new_inputs = []
    for input in node.inputs:
        if opt.check_chain(input, T.fill):
            model, filling = input.owner.inputs
            if encompasses_broadcastable(reference.type.broadcastable,
                                         filling.type.broadcastable):
                new_inputs.append(filling)
                continue
        new_inputs.append(input)

    if new_inputs == node.inputs:
        return False
    return node.op.make_node(*new_inputs).outputs

register_canonicalize(local_fill_cut)

register_canonicalize(gof.OpRemove(T.tensor_copy), name='remove_tensor_copy' )

@gof.local_optimizer([None, T.fill])
def local_fill_sink(node):
    """
    f(fill(a, b), fill(c, d), e) -> fill(a, fill(c, f(b, d, e)))
    """
    if not (node.op and isinstance(node.op, T.Elemwise) and node.op != T.fill):
        return False
    models = []
    inputs = []
    for input in node.inputs:
        if input.owner and input.owner.op == T.fill:
            models.append(input.owner.inputs[0])
            inputs.append(input.owner.inputs[1])
        else:
            inputs.append(input)
    if inputs == node.inputs:
        return False
    c = node.op(*inputs)
    for model in models:
        c = T.fill(model, c)
    return [c]

register_canonicalize(local_fill_sink)


################
# Canonization #
################

class Canonizer(gof.LocalOptimizer):
    """
    Simplification tool.

    Usage: Canonizer(main, inverse, reciprocal, calculate)
    
    * main: a suitable Op class that is commutative, associative and
            takes one to an arbitrary number of inputs, e.g. add or
            mul
    * inverse: an Op class such that inverse(main(x, y), y) == x
               e.g. sub or true_div
    * reciprocal: a function such that main(x, reciprocal(y)) ==
                  inverse(x, y) e.g. neg or inv

    * calculate: function that takes a list of numpy.ndarray instances
                 for the numerator, another list for the denumerator,
                 and calculates inverse(main(*num), main(*denum)). It
                 takes a keyword argument, aslist. If True, the value
                 should be returned as a list of one element, unless
                 the value is such that value = main(). In that case,
                 the return value should be an empty list.

    The variable is a local_optimizer. It is best used with a TopoOptimizer in
    in_to_out order.

    Examples:
      T = theano.tensor
      add_canonizer = Canonizer(T.add, T.sub, T.neg, lambda n, d: sum(n) - sum(d))
      mul_canonizer = Canonizer(T.mul, T.true_div, T.inv, lambda n, d: prod(n) / prod(d))
    
    Examples of optimizations mul_canonizer can perform:
      x / x -> 1
      (x * y) / x -> y
      x / y / x -> 1 / y
      x / y / z -> x / (y * z)
      x / (y / z) -> (x * z) / y
      (a / b) * (b / c) * (c / d) -> a / d
      (2.0 * x) / (4.0 * y) -> (0.5 * x) / y
      2 * x / 2 -> x
      x * y * z -> Elemwise(T.mul){x,y,z} #only one pass over the memory.
                !-> Elemwise(T.mul){x,Elemwise(T.mul){y,z}}
    """

    def __init__(self, main, inverse, reciprocal, calculate, use_reciprocal = True):
        self.main = main
        self.inverse = inverse
        self.reciprocal = reciprocal
        self.calculate = calculate
        self.use_reciprocal = use_reciprocal

    def tracks(self):
        return [[self.main, None], [self.inverse, None], [self.reciprocal, None]]

    def get_num_denum(self, input):
        """
        This extract two lists, num and denum, such that the input is:
        self.inverse(self.main(*num), self.main(*denum)). It returns
        the two lists in a (num, denum) pair.

        For example, for main, inverse and reciprocal = *, / and inv(),

        input -> returned value (num, denum)

        x*y -> ([x, y], [])
        inv(x) -> ([], [x])
        inv(x) * inv(y) -> ([], [x, y])
        x*y/z -> ([x, y], [z])
        log(x) / y * (z + x) / y -> ([log(x), z + x], [y, y])
        (((a / b) * c) / d) -> ([a, c], [b, d])
        a / (b / c) -> ([a, c], [b])
        log(x) -> ([log(x)], [])
        x**y -> ([x**y], [])
        x * y * z -> ([x, y, z], [])

        """

        # This function is recursive.
        # The idea is that there is a get_num_denum recursion in which the internal ops are all
        # one of (main, inverse, reciprocal, DimShuffle) and the internal data nodes all have
        # the dtype of the 'input' argument. The leaf-Variables of the graph covered by the
        # recursion may be of any Variable type.

        if len(input.clients) > 1:
            # this logic is too conservative, but doing it is better than not doing it.
            #
            # we don't want to canonize a subgraph that we will need to compute anyway for the other clients.
            # This check is too conservative because if the other clients are also in the subgraph we are canonizing,
            # then we should [probably?] recurse anyway.
            return [input], []

        if input.owner is None or input.owner.op not in [self.main, self.inverse, self.reciprocal]:
            if input.owner and isinstance(input.owner.op, T.DimShuffle):
                # If input is a DimShuffle of some input which does something like this:
                # * change a vector of length N into a 1xN row matrix
                # * change a scalar into a 1x1x1 tensor
                # * in general, complete the shape of a tensor with broadcastable 1s to the *left*
                # Then we will simply discard the DimShuffle and return the num/denum of its input
                dsn = input.owner    # dimshuffle node
                dsop = dsn.op        # dimshuffle op
                dsi0 = dsn.inputs[0] # the first input of the dimshuffle i.e. the ndarray to redim

                # The compatible order is a DimShuffle "new_order" of the form:
                # ('x', ..., 'x', 0, 1, 2, ..., dimshuffle_input.type.ndim)

                # That kind of DimShuffle only adds broadcastable
                # dimensions on the left, without discarding any
                # existing broadcastable dimension and is inserted
                # automatically by Elemwise when the inputs have
                # different numbers of dimensions (hence why we can
                # discard its information - we know we can retrieve it
                # later on).
                compatible_order = ('x',) * (input.type.ndim - dsi0.type.ndim) + tuple(range(dsi0.type.ndim))
                if dsop.new_order == compatible_order:
                    # If the "new_order" is the one we recognize,
                    # we return the num_denum of the dimshuffled input.
                    return self.get_num_denum(input.owner.inputs[0])
                else:
                    # This is when the input isn't produced by main, inverse or reciprocal.
                    return [input], []
            else:
                return [input], []
        num = []
        denum = []
        parent = input.owner

        # We get the (num, denum) pairs for each input
        #pairs = [self.get_num_denum(input2) if input2.type.dtype == input.type.dtype else ([input2], []) for input2 in parent.inputs]
        pairs = [self.get_num_denum(input2) for input2 in parent.inputs]

        if parent.op == self.main:
            # If we have main(x, y), numx, denumx, numy and denumy
            # then num is concat(numx, numy) and denum is concat(denumx, denumy)
            # note that main() can have any number of arguments >= 0
            # concat is list concatenation
            num = reduce(list.__iadd__, map(operator.itemgetter(0), pairs))
            denum = reduce(list.__iadd__, map(operator.itemgetter(1), pairs))
        elif parent.op == self.inverse:
            # If we have inverse(x, y), numx, denumx, numy and denumy
            # then num is concat(numx, denumy) and denum is concat(denumx, numy)
            # note that inverse() is binary
            num = pairs[0][0] + pairs[1][1]
            denum = pairs[0][1] + pairs[1][0]
        elif parent.op == self.reciprocal:
            # If we have reciprocal(x), numx, denumx
            # then num is denumx and denum is numx
            # note that reciprocal() is unary
            num = pairs[0][1]
            denum = pairs[0][0]
        return num, denum

    def merge_num_denum(self, num, denum):
        """
        Utility function which takes two lists, num and denum, and
        returns something which is equivalent to inverse(main(*num),
        main(*denum)), but depends on the length of num and the length
        of denum (in order to minimize the number of operations).

        Let n = len(num) and d = len(denum):

        n=0, d=0: neutral element (given by self.calculate([], []))
                  (for example, this would be 0 if main is addition
                  and 1 if main is multiplication)
        n=1, d=0: num[0]
        n=0, d=1: reciprocal(denum[0])
        n=1, d=1: inverse(num[0], denum[0])
        n=0, d>1: reciprocal(main(*denum))
        n>1, d=0: main(*num)
        n=1, d>1: inverse(num[0], main(*denum))
        n>1, d=1: inverse(main(*num), denum[0])
        n>1, d>1: inverse(main(*num), main(*denum))

        Given the values of n and d to which they are associated, all
        of the above are equivalent to:
        inverse(main(*num), main(*denum))
        """

        ln, ld = len(num), len(denum)
        if not ln and not ld:
            return T.as_tensor_variable(self.calculate([], []))
        if not ln:
            if self.use_reciprocal:
                return self.reciprocal(self.merge_num_denum(denum, []))
            else:
                ln = [self.calculate([], [], aslist = False)]
        if not ld:
            if ln == 1:
                if isinstance(num[0], gof.Variable):
                    return num[0]
                else:
                    return T.as_tensor_variable(num[0])
            else:
                return self.main(*num)
        return self.inverse(self.merge_num_denum(num, []),
                            self.merge_num_denum(denum, []))

    @classmethod
    def get_constant(cls, v):
        """

        Returns a numeric constant if v is a gof.Constant or, well, a
        numeric constant. If v is a plain Variable, returns None.

        """
        if isinstance(v, N.generic):
            return v # doesn't the not hasattr() condition below catch this?
        if isinstance(v, gof.Constant):
            return v.data
        if not hasattr(v, 'owner'):
            return v

#         NOTE: the following code was buggy, but while I was fixing
#         it I realized it is probably made useless by constant
#         folding, so screw that. Commented-out code is the half-fixed
#         version.

#         if v.owner and isinstance(v.owner.op, DimShuffle):
#             # see the comments in get_num_denum
#             # TODO: this should apply the 
#             dsn = v.owner
#             dsop = dsn.op
#             dsi0 = dsn.inputs[0]
#             compatible_order = ('x',) * (input.type.ndim - dsi0.type.ndim) + tuple(range(dsi0.type.ndim))
#             if dsop.new_order == compatible_order:
#                 return cls.get_constant(v.owner.inputs[0])

        return None

    def simplify(self, num, denum):
        """
        Shorthand for: self.simplify_constants(*self.simplify_factors(num, denum))
        """
        return self.simplify_constants(*self.simplify_factors(num, denum))

    def simplify_factors(self, num, denum):
        """
        For any Variable r which is both in num and denum, removes it
        from both lists. Modifies the lists inplace. Returns the
        modified lists. For example:

        [x], [x] -> [], []
        [x, y], [x] -> [y], []
        [a, b], [c, d] -> [a, b], [c, d]
        """
        for v in list(num):
            if v in denum:
                num.remove(v)
                denum.remove(v)
        return num, denum

    def simplify_constants(self, orig_num, orig_denum):
        """

        Finds all constants in orig_num and orig_denum (using
        get_constant) and puts them together into a single
        constant. The constant is inserted as the first element of the
        numerator. If the constant is the neutral element, it is
        removed from the numerator. Examples:

        Let main be multiplication:

        [2, 3, x], [] -> [6, x], []
        [x, y, 2], [4, z] -> [0.5, x, y], [z]
        [x, 2, y], [z, 2] -> [x, y], [z]
        """

        # Lists representing the numerator and denumerator
        num, denum = list(orig_num), list(orig_denum)
        out_type = self.merge_num_denum(orig_num, orig_denum).type

        # Lists representing the *constant* elements of num and denum
        numct, denumct = [], []
        
        for v in orig_num:
            ct = self.get_constant(v)
            if ct is not None:
                # We found a constant in the numerator!
                # We remove it from num
                num.remove(v)
                # We add it to numct
                numct.append(ct)
        for v in orig_denum:
            ct = self.get_constant(v)
            if ct is not None:
                denum.remove(v)
                denumct.append(ct)

        if self.use_reciprocal or num:
            # This will calculate either:
            # [inverse(main(*numct), main(*denumct))]
            # [] - if inverse(main(*numct), main(*denumct)) is the neutral element
            ct = self.calculate(numct, denumct, aslist = True, out_type=out_type)
        else:
            # This happens if we don't allow the reciprocal and the
            # numerator is empty. That means we will need to represent
            # reciprocal(x) like inverse(neutral_element, x) so
            # we can't allow ct == []
            # TODO: why is this branch needed when merge_num_denum does it for us?
            ct = [self.calculate(numct, denumct, aslist = False, out_type=out_type)]
        # TODO: why are we not wrapping ct in a gof.Constant right now?

        if orig_num and len(numct) == 1 and len(denumct) == 0 and ct and N.all(ct == self.get_constant(orig_num[0])):
            # this is an important trick :( if it so happens that:
            # * there's exactly one constant on the numerator and none on the denominator
            # * it's not the neutral element (ct is an empty list in that case)
            # * the constant is the same as the first argument in the numerator
            # Then we return very exactly the original num/denum
            # If we don't do that the optimizer will just loop infinitely because
            # it will not catch on that there are no changes to be made and everytime
            # it will want to replace something by the same thing...
            return orig_num, orig_denum
        return ct + num, denum

    def transform(self, node):
        op = node.op
        if op not in [self.main, self.inverse, self.reciprocal]:
            return False
        
        inputs = node.inputs
        out = node.outputs[0]
        assert len(node.outputs) == 1

        # check if any of the clients of this node would be part of this canonized graph...
        # if so, we do nothing and wait for them to be transformed.
        def _bypass_dimshuffle(n):
            if isinstance(n.op, DimShuffle) and len(n.outputs[0].clients) <= 1:
                return _bypass_dimshuffle(n.outputs[0].clients.__iter__().next()[0])
            else:
                return n
        for c,c_idx in out.clients:
            if c=='output': continue
            if _bypass_dimshuffle(c).op in [self.main, self.inverse, self.reciprocal]:
                return False
            
        # Here we make the canonical version of the graph around this node
        # See the documentation of get_num_denum and simplify
        orig_num, orig_denum = self.get_num_denum(node.outputs[0])
        num, denum = list(orig_num), list(orig_denum)
        num, denum = self.simplify(num, denum)

        def same(x, y):
            return len(x) == len(y) and all(N.all(xe == ye) for xe, ye in zip(x, y))

        if same(orig_num, num) and same(orig_denum, denum):
            # We return False if there are no changes
            return False

        new = self.merge_num_denum(num, denum)
        if new.type.dtype != out.type.dtype:
            #new = T.fill(out, new)
            elem_op = T.Elemwise(scalar.Identity(scalar.specific_out(getattr(scalar, out.type.dtype))))
            new = elem_op(new)

        assert (new.type == out.type) == (not (new.type != out.type))

        if not (new.type == out.type):
            new = _fill_chain(new, node.inputs)[0]

        if new.type == out.type:
            return [new]
        else:
            _logger.warning(' '.join(('CANONIZE FAILED: new, out = ', new, ',', out, 'types',
                new.type, ',', out.type)))
            return False

    def __str__(self):
        return getattr(self, 'name', 'Canonizer(%s, %s, %s)' % (self.main, self.inverse, self.reciprocal))


def mul_calculate(num, denum, aslist=False, out_type=None):
    if not num and not denum:
        # Smallest 1 possible.
        if aslist:
          return []
        else:
          return N.int8(1)

        #return [] if aslist else N.int8(1)
    # Make sure we do not accidently upcast data types.
    if out_type is None:
        # TODO: remove this error-causing heuristic
        if num:
          first = num[0]
        else:
          first = denum[0]
        #first = num[0] if num else denum[0]
        one = N.asarray(first).dtype.type(1)
    else:
        one = N.asarray(1, dtype=out_type.dtype)
    v = reduce(N.multiply, num, one) / reduce(N.multiply, denum, one)
    if aslist:
        if N.all(v == 1):
            return []
        else:
            return [v]
    return v

local_mul_canonizer = Canonizer(T.mul, T.true_div, T.inv, mul_calculate, False)
register_canonicalize(local_mul_canonizer, name = 'local_mul_canonizer')

@gof.local_optimizer([T.neg])
def local_neg_to_mul(node):
    if node.op == T.neg:
        return [T.mul(-1, node.inputs[0])]
register_canonicalize(local_neg_to_mul)

@register_specialize
@gof.local_optimizer([])
def local_sum_mul_by_scalar(node):
    """sum(scalar * smth) -> scalar * sum(smth)
    """
    # TODO: if the the thing inside the Sum is a division, 
    # we should get at the numerator....
    if isinstance(node.op, T.Sum):
        thing_summed, = node.inputs
        if thing_summed.owner and thing_summed.owner.op == T.mul:
            terms = thing_summed.owner.inputs
            scalars = [t.dimshuffle() for t in terms if numpy.all(t.type.broadcastable)]
            non_scalars = [t for t in terms if not numpy.all(t.broadcastable)]
            if scalars:
                if len(scalars) > 1:
                    if len(non_scalars) > 1:
                        return [T.mul(T.mul(*scalars), node.op(T.mul(*non_scalars)))]
                    elif len(non_scalars) == 1:
                        return [T.mul(T.mul(*scalars), node.op(non_scalars[0]))]
                    else:
                        return [T.mul(*scalars)]
                else:
                    if len(non_scalars) > 1:
                        return [T.mul(scalars[0], node.op(T.mul(*non_scalars)))]
                    elif len(non_scalars) == 1:
                        return [T.mul(scalars[0], node.op(non_scalars[0]))]
                    else:
                        return [scalars[0]]
        if thing_summed.owner and thing_summed.owner.op == T.neg:
            return [T.neg(node.op(thing_summed.owner.inputs[0]))]

@gof.local_optimizer([T.mul])
def local_mul_to_neg(node):
    if node.op == T.mul and N.all(local_mul_canonizer.get_constant(node.inputs[0]) == -1.0):
        return [-local_mul_canonizer.merge_num_denum(node.inputs[1:], [])]
    else:
        return False
register_specialize(local_mul_to_neg)

@register_specialize
@gof.local_optimizer([T.neg])
def local_neg_neg(node):
    # other specializations shouldn't put this in, 
    # but sometimes they do
    if node.op == T.neg:
        if node.inputs[0].owner and node.inputs[0].owner.op == T.neg:
            return [node.inputs[0].owner.inputs[0]]

@register_specialize
@gof.local_optimizer([T.neg])
def local_neg_div_neg(node):
    """- (-a / b) -> a / b

    Also performs - (c / b) -> ((-c) / b) when c is a scalar constant.
    """
    if node.op == T.neg:
        if node.inputs[0].owner and node.inputs[0].owner.op == T.true_div:
            frac = node.inputs[0]
            num, denom = frac.owner.inputs
            if num.owner and num.owner.op == T.neg:
                if len(frac.clients) == 1:
                    # No other clients of the original division
                    new_num = num.owner.inputs[0]
                    return [T.true_div(new_num, denom)]
            elif numpy.all(num.broadcastable) and isinstance(num, gof.Constant):
                if len(frac.clients) == 1:
                    new_num = -num.data
                    return [T.true_div(new_num, denom)]


@gof.local_optimizer([T.mul])
def local_mul_zero(node):
    """As part of canonicalization, we replace multiplication by zero with zero.
    """
    if node.op == T.mul:
        otype = node.outputs[0].type

        for i in node.inputs:
            try:
                value = get_constant_value(i)
            except TypeError:
                continue
            #print 'MUL by value', value, node.inputs
            if N.all(value == 0):
                #print '... returning zeros'
                return _fill_chain(N.asarray(0, dtype=otype.dtype), node.inputs)
register_canonicalize(local_mul_zero)

@gof.local_optimizer([T.true_div])
def local_div_to_inv(node):
    if node.op == T.true_div and N.all(local_mul_canonizer.get_constant(node.inputs[0]) == 1.0):
        return [T.inv(local_mul_canonizer.merge_num_denum(node.inputs[1:], []))]
    else:
        return False
register_specialize(local_div_to_inv)

@gof.local_optimizer([T.inv])
def local_inv_canon(node):
    if node.op == T.inv:
        return [T.pow(node.inputs[0], -1.0)]
    else:
        return False
register_canonicalize(local_inv_canon)

@gof.local_optimizer([T.pow])
def local_pow_canonicalize(node):
    if node.op == T.pow:
        if N.all(local_mul_canonizer.get_constant(node.inputs[1]) == 1.0):
            return [T.fill(node.inputs[1], node.inputs[0])]
        if N.all(local_mul_canonizer.get_constant(node.inputs[1]) == 0.0):
            #extra fills here are to make sure the size of the output stays constant.
            return [T.fill(node.inputs[0], T.fill(node.inputs[1], 1.0))]
    else:
        return False
register_canonicalize(local_pow_canonicalize)

@gof.local_optimizer([T.pow])
def local_pow_specialize(node):
    #here, we are past the point of canonicalization, so we don't want to put in un-necessary fills.
    if node.op == T.pow:
        #the idea here is that we have pow(x, y)
        xsym = node.inputs[0]
        ysym = node.inputs[1]
        y = local_mul_canonizer.get_constant(ysym)
        if (y is not None) \
                and encompasses_broadcastable(xsym.type.broadcastable, ysym.type.broadcastable):
            if N.all(y == 2.0):
                return [T.sqr(xsym)]
            if N.all(y == 1.0):
                return [xsym]
            if N.all(y == 0.0):
                return [T.fill(xsym, 1.0)]
            if N.all(y == 0.5):
                return [T.sqrt(xsym)]
            if N.all(y == -0.5):
                return [T.inv(T.sqrt(xsym))]
            if N.all(y == -1.0):
                return [T.inv(xsym)]
            if N.all(y == -2.0):
                return [T.inv(T.sqr(xsym))]
    else:
        return False
register_specialize(local_pow_specialize)

@gof.local_optimizer([T.mul])
def local_mul_specialize(node):
    def fill_chain(v):
        return _fill_chain(v, node.inputs)
    #here, we are past the point of canonicalization, so we don't want to put in un-necessary fills.
    if node.op == T.mul:
        #the idea here is that we have pow(x, y)
        neg = False
        new_inputs = []
        for input in node.inputs:
            y = local_mul_canonizer.get_constant(input)
            if N.all(y == 1.0):
                continue
            elif N.all(y == -1.0):
                neg ^= True #toggles
            elif N.all(y == 0.0):
                return fill_chain(input)
            else:
                new_inputs.append(input)
        if len(new_inputs) < len(node.inputs):
            if len(new_inputs) == 0:
                if neg:
                   newval = -y.flatten()[0]
                else:
                   newval = y.flatten()[0]
                #newval = -y.flatten()[0] if neg else y.flatten()[0]
                return fill_chain(T.TensorConstant(T.TensorType(dtype=node.outputs[0].type.dtype,
                    broadcastable = [True] * node.outputs[0].ndim), N.asarray(newval)))

            if len(new_inputs) == 1:
              if neg:
                msg = -new_inputs[0]
              else:
                msg = new_inputs[0]
              return fill_chain(msg)
              #  return fill_chain(-new_inputs[0] if neg else new_inputs[0])
            else:
                if neg:
                  msg = -T.mul(*new_inputs)
                else:
                  msg = T.mul(*new_inputs)

                #return fill_chain(-T.mul(*new_inputs) if neg else \
                #        T.mul(*new_inputs))
    else:
        return False
register_specialize(local_mul_specialize)

@gof.local_optimizer([T.add])
def local_add_specialize(node):
    def fill_chain(v):
        return _fill_chain(v, node.inputs)

    def get_constant_through_fills_and_subtensors(v):
        if v.owner is not None:
            if v.owner.op == T.fill:
                assert len(v.owner.inputs) == 2
                return get_constant_through_fills_and_subtensors(v.owner.inputs[1])
            if isinstance(v.owner.op, T.DimShuffle):
                assert len(v.owner.inputs) == 1
                return get_constant_through_fills_and_subtensors(v.owner.inputs[0])
        elif hasattr(v, 'data'):
            return v.data
        else:
            return v

    #here, we are past the point of canonicalization, so we don't want to put in un-necessary fills.
    if node.op == T.add:
        new_inputs = []
        for input in node.inputs:
            y = get_constant_through_fills_and_subtensors(input)
            if N.all(y == 0.0):
                continue
            else:
                new_inputs.append(input)

        if len(new_inputs) < len(node.inputs):
            if len(new_inputs) == 0:
                #we got rid of the entire expression!
                return fill_chain(T.TensorConstant(T.TensorType(dtype=node.outputs[0].type.dtype,
                    broadcastable = [True] * node.outputs[0].ndim), N.asarray(0)))

            if len(new_inputs) == 1:
                return fill_chain(new_inputs[0])
            else:
                return fill_chain(T.add(*new_inputs))
    else:
        return False
register_specialize(local_add_specialize)

# neg_to_mul = out2in(gof.LocalOptGroup(local_neg_to_mul))
# mul_to_neg = out2in(gof.LocalOptGroup(local_mul_to_neg))

mul_canonizer = in2out(gof.LocalOptGroup(local_mul_canonizer, local_fill_cut, local_fill_sink))

@register_specialize
@gof.local_optimizer([T.log])
def local_log1p(node):
    # log(1+exp(x)) -> log1p(x)
    if node.op == T.log:
        log_arg, = node.inputs
        if log_arg.owner and log_arg.owner.op == T.add:
            add_inputs = log_arg.owner.inputs
            consts = [0]
            fills = []
            nonconsts = []
            for add_in in add_inputs:
                try:
                    v, f = get_constant_value(add_in, fill=True)
                    consts.append(v)
                    fills.extend(f)
                except:
                    nonconsts.append(add_in)
            if nonconsts:
                if numpy.allclose(numpy.sum(consts), 1):
                    if len(nonconsts)==1:
                        return _fill_chain(T.log1p(nonconsts[0]), fills)
                    else:
                        return _fill_chain(T.log1p(T.add(*nonconsts)), fills)


def add_calculate(num, denum, aslist = False, out_type=None):
    #TODO: make sure that this function and mul_calculate are similar
    if out_type is None:
      zero = 0.0
    else:
      zero = N.asarray(0, dtype=out_type.dtype)
    #zero = 0.0 if out_type is None else N.asarray(0, dtype=out_type.dtype)
    v = reduce(N.add, num, zero) - reduce(N.add, denum, zero)
    if aslist:
        if N.all(v == 0):
            return []
        else:
            return [v]
    return v

local_add_canonizer = Canonizer(T.add, T.sub, T.neg, add_calculate)
add_canonizer = in2out(gof.LocalOptGroup(local_add_canonizer, local_fill_cut, local_fill_sink))

register_canonicalize(local_add_canonizer, name = 'local_add_canonizer')


##################
# Distributivity #
##################


def distribute_greedy(pos_pairs, neg_pairs, num, denum, minscore = 0):
    # each pair in pos_pairs and neg_pairs is a num/denum pair. this
    # function attempts to add num and denum to the corresponding parts
    # of each pair, and counts how many multiplications/divisions can
    # be saved in that way.

    # each division is counted like div_cost multiplications
    # (typically, division costs more so we are willing to multiply more
    # in order to divide less)
    # 1.5 was obtained through an informal test and may very well be
    # platform dependent
    div_cost = 1.5

    score = len(num) + div_cost * len(denum) # score is number of operations saved, higher is better
    new_pos_pairs = list(itertools.starmap(local_mul_canonizer.simplify,
                                           [(n+num, d+denum) for (n, d) in pos_pairs]))
    new_neg_pairs = list(itertools.starmap(local_mul_canonizer.simplify,
                                           [(n+num, d+denum) for (n, d) in neg_pairs]))
    for (n, d), (nn, dd) in zip(pos_pairs + neg_pairs, new_pos_pairs + new_neg_pairs):
        # We calculate how many operations we are saving with the new num and denum
        score += len(n) + div_cost * len(d) - len(nn) - div_cost * len(dd)
    if score <= minscore:
        # the change is not applied because it adds too many operations
        return False, pos_pairs, neg_pairs
    return True, new_pos_pairs, new_neg_pairs

def attempt_distribution(factor, num, denum):
    # we try to insert each num and each denum in the factor
    # returns: changes?, new_factor, new_num, new_denum
    # if there are changes, new_num and new_denum contain all the numerators
    # and denumerators that could not be distributed in the factor
    pos, neg = local_add_canonizer.get_num_denum(factor)
    if len(pos) == 1 and not neg:
        return False, factor, num, denum
    pos_pairs = map(local_mul_canonizer.get_num_denum, pos)
    neg_pairs = map(local_mul_canonizer.get_num_denum, neg)
    change = False
    for n in list(num):
        success, pos_pairs, neg_pairs = distribute_greedy(pos_pairs, neg_pairs, [n], [])
        if success:
            change = True
            num.remove(n)
    for d in list(denum):
        success, pos_pairs, neg_pairs = distribute_greedy(pos_pairs, neg_pairs, [], [d])
        if success:
            change = True
            denum.remove(d)
    if not change:
        return change, factor, num, denum
    else:
        return change, local_add_canonizer.merge_num_denum(
            list(itertools.starmap(local_mul_canonizer.merge_num_denum, pos_pairs)),
            list(itertools.starmap(local_mul_canonizer.merge_num_denum, neg_pairs))), num, denum

@gof.local_optimizer([T.mul, T.add, T.mul], [T.mul, T.sub, T.mul],
                     [T.mul, T.add, T.true_div], [T.mul, T.sub, T.true_div])
def local_greedy_distributor(node):
    """
    This optimization tries to apply distributivity of multiplication
    to addition in order to reduce the number of multiplications
    and/or divisions that must be done. The algorithm weighs division
    more than multiplication to account for the former's slightly
    greater computational cost.

    The following expressions are simplified:
    1. ((a/x + b/y) * x * y) --> a*y + b*x
    2. ((a/x + b) * x) --> a + b*x

    The following expressions are not simplified:
    3. ((a + b) * x) -/-> a*x + b*x

    This optimization aims to reduce computational cost. It may also
    increase numerical stability, e.g. when x and/or y tend to 0 in
    example 1.
    """

    out = node.outputs[0]
    num, denum = local_mul_canonizer.get_num_denum(out)
    if len(num) == 1 and not denum:
        return False

    new_num, new_denum = [], []

    change = False

    for candidate in list(num):
        if candidate not in num:
            continue
        num.remove(candidate)
        _change, candidate, num, denum = attempt_distribution(candidate, num, denum)
        change |= _change
        new_num.append(candidate)

    for candidate in list(denum):
        if candidate not in denum:
            continue
        denum.remove(candidate)
        _change, candidate, denum, num = attempt_distribution(candidate, denum, num)
        change |= _change
        new_denum.append(candidate)

    if not change:
        return False

    new_num += num
    new_denum += denum

    rval = local_mul_canonizer.merge_num_denum(new_num, new_denum)

    if not (rval.type == out.type):
        #WHY DOES THIS HAPPEN?
        return False

    return [rval]

register_canonicalize(local_greedy_distributor)



@gof.local_optimizer([None])
def constant_folding(node):
    for input in node.inputs:
        if not isinstance(input, gof.Constant):
            return False
    storage = [[None] for output in node.outputs]
    node.op.perform(node, [x.data for x in node.inputs], storage)
    msg = []
    for s, output in zip(storage, node.outputs):
        try:
            constant = output.type.Constant
        except:
            constant = gof.Constant
        msg += [constant(output.type, s[0])]
    return msg

register_canonicalize(constant_folding)
register_specialize(constant_folding)


inplace_matrix_transpose = T.DimShuffle([False,False], [1,0], inplace=True)
local_transposed_dot = gof.PatternSub((inplace_matrix_transpose, (T.dot, 'x', 'y')),
        (T.dot, (inplace_matrix_transpose, 'y'), (inplace_matrix_transpose, 'x')))
register_canonicalize(local_transposed_dot, name='local_transposed_dot')

# ###############
# # Loop fusion #
# ###############

def local_elemwise_fusion(node):
    """
    As part of specialisation, we fusion two consecutif elemwise op of the same shape.

    For mixed dtype, we let the Compise op do the cast. It let the C compile do the cast.
    The number of dimension is validated at call time by theano itself.

    """
    # META TODO:  PUT THESE THINGS IN TRAC, NOT TODO NOTES!!
    # TODO: use broadcast flag?

    # TODO: don't do this optimization as a localOptimizer.  Analyze the graph in terms of
    # elemwise subgraphs, and then replace each subgraph with a Composite version.

    # TODO: use malloc and copy to transfer arguments that don't fit within the parameter space
    # of 256 bytes
    #
    # TODO: Merge with multiple output to merge when an inputs have multiple clients. This can't be done with a local optimiser.
    # TODO: Related: Support composites with multiple outputs

    # TODO: Use Composite to combine Elemwise and Reduce operations.  We have to loop over the
    # data anyway... might as well sum it up while we're at it (this can be trickier than i'm
    # making it seound here. The data-traversal should be done contiguously, and the summing-up
    # might not be easy or worthwhile if the summation axis doesn't line up with a contiguous
    # dimension)

    if not isinstance(node.op, T.Elemwise):
        return False
    nb_elemwise=0
    inputs=[]#inputs of the new Elemwise op.
    s_inputs = []#inputs of the new scalar op.
    s_g=[]#graph of scalar, what will by done in the inner loop.
    for i in node.inputs:
        do_fusion = False
        catch = False
        if i.owner and isinstance(i.owner.op,T.Elemwise) and len(i.clients)<=1:
            #if the scalar_op don't have a c implementation, we skip its fusion to allow the fusion of the other ops.
            do_fusion=True
            try:
                s_input = [scalar.Scalar(x.dtype).make_variable() for x in i.owner.inputs]
                s_op=i.owner.op.scalar_op(*s_input)
                i.owner.op.scalar_op.c_code(s_op.owner,"test_presence_of_c_code",
                                            ["x" for x in i.owner.inputs],
                                            "z",{})
            except MethodNotDefined:
                catch = True
            except NotImplementedError:
                catch = True
            if catch:
                _logger.info("%s does not implement the c_code function. As well as being potentially slow, this disables loop fusion of this op." % str(i.owner.op.scalar_op))
                do_fusion=False

        if do_fusion:
            nb_elemwise+=1
            inputs.extend(i.owner.inputs)
            s_inputs.extend(s_input)
            s_g.append(s_op)
        else:
            inputs.append(i)
            s=scalar.Scalar(i.dtype).make_variable()
            s_inputs.append(s)
            s_g.append(s)

    #if no inputs have are an elemwise, there is nothing to fuse.
    if nb_elemwise==0:
#        print "local_elemwise_fusion: no elemwise in inputs. Nothing to fuse."
        return False

    otype = node.outputs[0].type
    s_new_out=node.op.scalar_op(*s_g)
    try:
        s_new_out.owner.op.c_code(s_new_out.owner, "test_presence_of_c_code",
                         ["x" for x in s_g],
                         "z",{}) 
    except MethodNotDefined:
        _logger.info("%s does not implement the c_code function. As well as being potentially slow, this disables loop fusion of this op." % str(s_new_out.owner.op))
        return False
    except NotImplementedError:
        _logger.info("%s does not implement the c_code function. As well as being potentially slow, this disables loop fusion of this op." % str(s_new_out.owner.op))
        return False

    #create the composite op.
    C = scalar.Composite(s_inputs,[s_new_out])

    #create the new node.
    n=T.Elemwise(C).make_node(*inputs)
    assert len(n.outputs)==1
    assert node.outputs[0].dtype==n.outputs[0].dtype

    # There is a hard limit of 256 bytes for the formal argument list to a GPU kernel function.
    # Here, we estimate how many bytes the new Op will need, and abort if it needs too much.
    if True:
        argument_limit = 200  # 256 didn't work, but a lower number did... so something funny
        # is going on 
        int_size = 4
        ptr_size = 4
        argument_size = 4 #for numels
        argument_size += int_size *  inputs[0].type.ndim # for the shape
        argument_size += sum((ptr_size + int_size * i.type.ndim) for i in n.inputs)
        argument_size += sum((ptr_size + int_size * i.type.ndim) for i in n.outputs)
        if argument_size >= argument_limit:
            _logger.warning('loop fusion failed because Op would exceed kernel argument limit.')
            return False

#    print "local_elemwise_fusion: FUSED",nb_elemwise+1,"elemwise!"
    return n.outputs

class FusionOptimizer(Optimizer):
    """Graph optimizer for Fusion of elemwise operations"""
    def __init__(self):
        Optimizer.__init__(self)

    def add_requirements(self, env):
        env.extend(toolbox.ReplaceValidate())
        env.extend(DestroyHandler())

    def apply(self, env):
        did_something = True
        while did_something:
            nodelist = list(env.toposort())
            did_something = False
            for node in nodelist:
                new_outputs = local_elemwise_fusion(node)
                if new_outputs:
                    assert len(new_outputs) == len(node.outputs)
                    try:
                        env.replace_all_validate(
                                zip(node.outputs, new_outputs),
                                reason = self.__class__.__name__)
                        did_something = True
                        break
                    except InconsistencyError, e:
                        #TODO: retry other applications of gemm (see comment in _gemm_from_node
                        pass


if config.config.getboolean('tensor_opt.local_elemwise_fusion'):
    _logger.debug("enabling optimization fusion elemwise in fast_run")
    compile.optdb.register('elemwise_fusion', FusionOptimizer(), 71.00, 'fast_run', 'fusion', 'local_elemwise_fusion')
else:
    _logger.debug("not enabling optimization fusion elemwise in fast_run")
    compile.optdb.register('elemwise_fusion', FusionOptimizer(), 71.00, 'fusion', 'local_elemwise_fusion')


