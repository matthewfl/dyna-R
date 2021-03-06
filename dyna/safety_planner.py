
from .interpreter import *
from .terms import BuildStructure, CallTerm, Evaluate, ReflectStructure

# mode_cache = {
#     term: {
#         in_mode (as a tuple of the arguments) : (out_mode (as a tuple of the arguments), has_delayed_constraints, basic_is_finite, dependants_set)
#     }
# }

# out_mode is a boolean mode for which variables are bound
# has_delayed_constraints is an upper bound on if this has delayed constraints.  (There might not be delayed constraints due to something value dependent)
# basic_is_finite:  Start with the assumption that everything is not finite, and then if it is only calling builtins and other finite things, then it can
#                   be marked as finite
# dependants_sets: tracks which things need to be updated if there are


# the memoized values could change what can run, as atm the memoized expressions require that the groundings are fully grounded before they can run.
# that means this should look at what modes are proposed for the memoized expressions such that it can determine if the mode is too strict
# in the case that a memoized mode is not supported, I suppose that this should either change the memoized mode so
# that it can run what it needs.


class SafetyPlanner:

    def __init__(self, get_rexpr):
        # this should have some way of copying stuff so that we can check that a code change will not violate the declared queries
        self.mode_cache = {}
        self._agenda = []
        self.get_rexpr = get_rexpr

    def _lookup(self, term, mode, push_computes):
        cache = self.mode_cache.get(term)

        term_name, arg_names = term  # the name is packed in with the exposed variables
        assert len(arg_names) == len(mode)

        if cache is None:
            # then we have to construct the initial cache the initial state is
            # that all expressions are going to come back as ground.  But then
            # we are going to mark that we have to reprocess the agenda for this
            # expression
            cache = {(False,)*len(mode): ((True,)*len(mode), False, False, set())}
            self.mode_cache[term] = cache
            self._push_agenda((term, (False,)*len(mode)))

        if mode in cache:
            return cache[mode]

        # first we check if there is a more free mode that matches the
        # requirements for this mode but returns that all of its arguments will
        # be ground
        for kk, v in cache.items():
            if all(m or not k for m,k in zip(mode, kk)) and all(v[0]):
                return v

        if push_computes:
            # if we are unable to find it, then we guess that it could fully
            # ground the arguments meaning that it returns without any delayed
            # constraints.  This will get checked via the agenda
            r = ((True,)*len(mode), False, False, set())
            cache[mode] = r
            self._push_agenda((term, mode))
            return r

    def _compute(self, term, mode):
        cache = self.mode_cache[term][mode]
        name = (term, mode)
        term_name, exposed_vars = term
        R = self.get_rexpr(term_name)  # this needs to not use the memoized or
                                       # compiled versions, but it can use the
                                       # optimized versions of a program
        out_mode = self._compute_R(R, exposed_vars, mode, name)

        if cache[0:-1] != out_mode:
            self.mode_cache[term][mode] = (*out_mode, set())
            for d in cache[-1]:
                self._push_agenda(d)  # these need to get reprocessed

    def _compute_R(self, R, exposed_vars, in_mode, name):
        # determine what the true out mode for this expression is by using
        # lookup and collecting the expressions that we are dependant on.

        bound_vars = Frame()  # just set the value of true in the case that something is bound
        push_computes = False
        has_remaining_delayed_constraints = False  # if there are constraints that we were unable to evaluate
        basic_is_finite = True  # an basic approximation that this is finite, that this only uses builtins or things that are not possibly calling itself

        def track_set(var):
            try:
                var.setValue(bound_vars, True)
            except UnificationFailure:  # if a constant, then it could throw
                pass

        for var, im in zip(exposed_vars, in_mode):
            if im:
                track_set(var)

        def walker(R):
            nonlocal push_computes, has_remaining_delayed_constraints, basic_is_finite
            if isinstance(R, Partition):
                # the partition requires that a variable is grounded out on all branches
                imode = tuple(v.isBound(bound_vars) for v in R._unioned_vars)
                unioned_modes = [True] * len(R._unioned_vars)
                for kk, cc in R._children.items():
                    for c in cc:
                        for var, k, im in zip(R._unioned_vars, kk, imode):
                            if k is not None or im:
                                track_set(var)
                            else:
                                var._unset(bound_vars)
                        walker(c)
                        for i, var in enumerate(R._unioned_vars):
                            if not var.isBound(bound_vars):
                                unioned_modes[i] = False
                for var, im, um in zip(R._unioned_vars, imode, unioned_modes):
                    if im or um:
                        track_set(var)
                    else:
                        var._unset(bound_vars)
            elif isinstance(R, ModedOp):
                # then we can just lookup the modes and determine if we are in
                # one of them.  In which case, then we
                mode = tuple(v.isBound(bound_vars) for v in R.vars)
                if mode in R.det or mode in R.nondet:
                    for v in R.vars:
                        track_set(v)
                else:
                    has_remaining_delayed_constraints = True
            elif isinstance(R, BuildStructure):
                if R.result.isBound(bound_vars):
                    for v in R.arguments:
                        track_set(v)
                elif all(v.isBound(bound_vars) for v in R.arguments):
                    track_set(R.result)
                else:
                    has_remaining_delayed_constraints = True
            elif isinstance(R, CallTerm):
                # then we need to look this expression up, but that is also
                # going to have to determine which variables are coming back or
                # what the mode is for those expressions.  In this case, we are
                arg_vars = tuple(sorted(R.var_map.keys()))  # these are the public variables that are exposed from an expression?
                mode = tuple(R.var_map[a].isBound(bound_vars) for a in arg_vars)

                l = self._lookup((R.term_ref, arg_vars), mode, push_computes)
                if l:
                    out_mode, has_remain, is_finite, tracking = l
                    if name:
                        tracking.add(name)  # track that we performed a read on this expression
                    if has_remain:
                        has_remaining_delayed_constraints = True
                    if not is_finite:
                        basic_is_finite = False

                    # track that this variable is now set
                    for av, rm in zip(arg_vars, out_mode):
                        if rm:
                            track_set(R.var_map[av])
                else:
                    has_remaining_delayed_constraints = True
            elif isinstance(R, Unify):
                if R.v1.isBound(bound_vars):
                    track_set(R.v2)
                elif R.v2.isBound(bound_vars):
                    track_set(R.v1)
                else:
                    has_remaining_delayed_constraints = True
            elif isinstance(R, Aggregator):
                walker(R.body)
                # we need to figure out if the arguments are bound sufficient
                # that we could run this expression.  I think that this is once
                # the resulting aggregated value is bound on all branches, then
                # it would have a value, so we can just use that?  But once the
                # head is bound is when we start trying to run the loop.

                if R.body_res.isBound(bound_vars):
                    track_set(R.result)
                else:
                    has_remaining_delayed_constraints = True
            elif isinstance(R, Evaluate):
                if R.term_var.isBound(bound_vars) and all(v.isBound(bound_vars) for v in R.extra_args):
                    # if there are non-ground variables, then anything might come back, so only when all ground can we be sure
                    # that due the the semantics of dyna that the resulting variable will be ground


                    assert False
                    # we can only say that there are no delayed constraints in
                    # the case that we enforce that methods don't come back with
                    # delayed constraints when all of its arguments are ground.
                    # We should just mark things that fail the +++ mode with all
                    # ground as failed and notify the user?

                    track_set(R.ret)
                else:
                    # we don't know in this case if it doesn't come back with something delayed as we don't know what is being called
                    has_remaining_delayed_constraints = True
            elif isinstance(R, ReflectStructure):
                assert False  # TODO: this should mark the different modes that this supports
                has_remaining_delayed_constraints = True
            else:
                for c in R.children:
                    walker(c)

        while True:
            last_binding = Frame(bound_vars)  # copy
            walker(R)
            walker(R)
            if last_binding == bound_vars:
                # then this has reached a fixed point for what can be bound, so
                # we stop at this point
                break

        has_remaining_delayed_constraints = False  # reset
        push_computes = True  # push that we would like the modes of the methods we are calling to be determiend if they could be better
        walker(R)
        walker(R)

        # now this needs to save the result to the cache and if there are
        # differences, then we also will need to push everything to the
        # agenda

        out_mode = tuple(v.isBound(bound_vars) for v in exposed_vars)

        #print(name, in_mode, out_mode, has_remaining_delayed_constraints, basic_is_finite)

        return out_mode, has_remaining_delayed_constraints, basic_is_finite

    def _process_agenda(self):
        while self._agenda:
            p = self._agenda.pop()
            self._compute(*p)

    def _push_agenda(self, n):
        if n not in self._agenda:  # use a set?
            self._agenda.append(n)

    def __call__(self, R, exposed_vars, in_mode):
        # do the safety planning for an R-expr
        while True:
            out_mode, has_delayed, basic_is_finite = self._compute_R(R, exposed_vars, in_mode, None)
            if not self._agenda:
                break  # meaning that nothing was pushed to work on in the processes of computing
            self._process_agenda()
        return out_mode, has_delayed, basic_is_finite

    def invalidate_term(self, term):
        # invalidate a term when the definition of it has changed.  We will push
        # changes to the agenda, but we are not going to eagerly process the
        # agenda instead leaving that for something else that wants to ensure different declared queries
        cache = self.mode_cache.get(term)
        if cache is not None:
            for mode in cache.keys():
                # these are going to need to be refreshed, so pushing them to
                # the agenda will make it such that they will get recomputed.
                # These changes could later propagate in the case that these
                # later are found to have different modes.
                self._push_agenda((term, mode))
