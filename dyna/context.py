# this file should probably be renamed to something like dynabase or something,
# it is holding the references to the different terms that are defined.



# maybe these should be imported later or not at all?
from .interpreter import *
from .terms import CallTerm, Evaluate, Evaluate_reflect, ReflectStructure, BuildStructure
from .guards import Assumption, AssumptionWrapper, AssumptionResponse
from .agenda import Agenda
from .optimize import run_optimizer
from .compiler import run_compiler, EnterCompiledCode
from .memos import rewrite_to_memoize, RMemo, AgendaMessage, process_agenda_message, MemoContainer
from .safety_planner import SafetyPlanner

from functools import reduce
import operator
import os

class SystemContext:
    """
    Represents the dyna system with the overrides for which expressions are going to be set and written
    """

    def __init__(self, parent=None):
        # the terms as the user defined them (before we do any rewriting) we can
        # not delete these, as we must keep around the origional definitions
        # so that we can recover in the case of "delete everything" etc
        self.terms_as_defined = {}

        # if there is some rewriting process that we have for terms, then we need to determine when these expressions are changing
        self.terms_as_optimized = {}

        # the memo tables that are wrapped around the terms.
        self.terms_as_memoized = {}

        # Dict[RExpr, CompiledRexprs]
        # when we compile a term this will be the resulting reference for that object
        self.terms_as_compiled = {}

        self.merged_expressions = {}

        self.term_assumptions = {}
        self.terms_as_defined_assumptions = {}

        self.agenda = Agenda()

        self.infered_constraints = []  # the constraints with generic versions that can be quickly matched to identify when something new can be infered
        self.infered_constraints_index = {}

        self.parent = parent

        self.safety_planner = SafetyPlanner(lambda term: self.lookup_term(term, ignore=('memos', 'compile')))

        self.stack_recursion_limit = 10

        if parent is None:
            # then we load the builtin operators
            from dyna.builtins import define_builtins
            define_builtins(self)
            # where we fallback for other defined expressions

            # load the prelude file
            with open(os.path.join(os.path.dirname(__file__), 'prelude.dyna'), 'r') as f:
                self.add_rules(f.read())
        else:
            assert False

        self.run_agenda()

    def term_as_defined_assumption(self, name):
        if name not in self.terms_as_defined_assumptions:
            self.terms_as_defined_assumptions[name] = Assumption(f'defined {name}')
        return self.terms_as_defined_assumptions[name]

    def term_assumption(self, name):
        if name not in self.term_assumptions:
            a = Assumption(name)
            self.term_assumptions[name] = a
            self.term_as_defined_assumption(name).track(a)
        return self.term_assumptions[name]

    def invalidate_term_assumption(self, name):
        # there needs to be a more refined assumption tracking for these terms
        # getting defined, eg, if the term is invalidated because a user
        # redefined the term and thus the semantics of the term have changed.
        # there is also if the optimizer or compiler has constructed a more
        # efficient expression, then we might want to incoperate that meaning
        # there should be an invalidation (notification) as a memo table
        # implementation would want to use the more efficient expression
        a = self.term_assumption(name)
        n = Assumption(name)
        self.term_as_defined_assumption(name).track(n)
        self.term_assumptions[name] = n
        a.invalidate()
        # this invalidates safety planning more than it should be.  as we are also calling this also happens in the case of something
        # getting memoized, which should not change the result of what modes are supported?
        self.safety_planner.invalidate_term(name)
        return n

    def invalidate_term_as_defined_assumption(self, name):
        a = self.term_as_defined_assumption(name)
        n = Assumption(f'defined: {name}')
        nt = Assumption(name)
        self.terms_as_defined_assumptions[name] = n
        self.term_assumptions[name] = nt
        n.track(nt)
        a.invalidate()
        self.safety_planner.invalidate_term(name)
        return n

    def delete_term(self, name, arity):
        a = (name, arity)
        if a in self.terms_as_defined:
            del self.terms_as_defined[a]
        if a in self.terms_as_optimized:
            del self.terms_as_optimized[a]
        if a in self.terms_as_memoized:
            del self.terms_as_memoized[a]
        if a in self.terms_as_compiled:
            del self.terms_as_compiled[a]
        if a in self.merged_expressions:
            del self.merged_expressions[a]  # TODO: is this what we want?

        # do invalidation last as we want anything that rechecks to get the new values
        self.invalidate_term_assumption(a)
        # if there is something in the parent, then maybe we should instead save
        # this as empty, that way we can track that we are resetting.  Though
        # maybe if we have fully overwritten something, then we are going to
        # track that?

    def define_term(self, name, arity, rexpr, *, redefine=False):
        assert (name, arity) not in self.terms_as_defined or redefine
        self.terms_as_defined[(name, arity)] = rexpr
        self.invalidate_term_assumption((name, arity))

    def add_to_term(self, name, arity, rexpr):
        # check that the aggregator is the same and the combine the expressions
        # together anything that depends on the value of the expression will
        # need to be invalided.
        a = (name, arity)
        if a not in self.terms_as_defined:
            self.terms_as_defined[a] = rexpr
        else:
            # then we need to combine these expressions together
            prev = self.terms_as_defined[a]
            # there should be an aggregator on the outer level, so we first check that, and it doesn't match then we are going to raise an error
            if not isinstance(prev, Aggregator) or not isinstance(rexpr, Aggregator) or prev.aggregator != rexpr.aggregator:
                raise RuntimeError("mismatch aggregator")
            # we are giong to rewrite the new expression to match the current expression
            nm = {}
            nm.update(dict(zip(rexpr.head_vars, prev.head_vars)))
            nm[rexpr.result] = prev.result
            nm[rexpr.body_res] = prev.body_res

            nr = rexpr.rename_vars(lambda x: nm.get(x,x))

            # merge the branches of the partition
            assert isinstance(prev.body, Partition) and isinstance(nr.body, Partition)
            assert prev.body._unioned_vars == nr.body._unioned_vars  # check that the orders are the same, otherwise this would require a more complicated merge

            # this is going to modify the branches of the currently stored body rather than create something new...sigh, I guess we also do this with the memos

            mt = prev.body._children
            for key, vals in nr.body._children.items():
                mt.setdefault(key, []).extend(vals)

            # if there is a memo table, then will mark these keys as needing to be refreshed
            memoized = self.terms_as_memoized.get(a)
            if memoized is not None:
                for child in memoized.all_children():
                    if isinstance(child, RMemo):
                        table = child.memos
                        for key, vals in nr.body._children.items():
                            msg = AgendaMessage(table=table, key=key)
                            self.agenda.push(lambda: process_agenda_message(msg))

        # track that this expression has changed, which can cause things to get recomputed/propagated to the agenda etc
        self.invalidate_term_as_defined_assumption(a)
        #self.invalidate_term_assumption(a)

    def lookup_term_aggregator(self, name, arity):
        a = (name, arity)
        if a not in self.terms_as_defined:
            return None
        t = self.terms_as_defined[a]
        if not isinstance(t, Aggregator):
            return None
        name = None
        from .aggregators import AGGREGATORS
        for k, v in AGGEGATORS.items():
            if v is t.aggregator:
                name = k
        return t.aggregator, k

    def memoize_term(self, name, kind='null', mem_variables=None):
        assert kind in ('unk', 'null', 'none')

        if mem_variables is not None:
            mem_variables = variables_named(*mem_variables)  # ensure these are cast to variables

        old_memoized = self.terms_as_memoized.get(name)

        if kind != 'none':
            R = self.lookup_term(name, ignore=('memo', 'assumption', 'assumption_defined'))

            # this really needs to call, but avoid hitting the memo wrapper that we
            # are going to add.  As in the case that the assumption is blown then we
            # are going to want to get a new version of the code.
            Rm = rewrite_to_memoize(R, mem_variables=mem_variables, is_null_memo=(kind == 'null'), dyna_system=self)
            self.terms_as_memoized[name] = Rm
        else:
            self.terms_as_memoized.pop(name)

        # invalidate and remove all assumptions
        self.invalidate_term_assumption(name)
        if old_memoized is not None:
            for child in old_memoized.all_children():
                if isinstance(child, RMemo):
                    # single to anything that was depending on this memo table that it no longer exists
                    child.memos.assumption.invalidate()

    def define_infered(self, required :RBaseType, added :RBaseType):
        z = (required, added)
        self.infered_constraints.append(z)

        # this is the constraint with the most number of variables attached, so
        # the thing that we are going to look for an index on
        ri = max(required.all_children(), key=lambda x: len(x.vars))
        self.infered_constraints_index.setdefault(type(ri), {}).setdefault(ri.weak_equiv()[0], []).append(z)

    def call_term(self, name, arity) -> RBaseType:
        # this should return a method call to a given term.
        # this should be lazy.

        m = {x:x for x in variables_named(*range(arity))}
        m[ret_variable] = ret_variable
        return CallTerm(m, self, (name, arity))

    def raw_call(self, name, arguments):
        t = self.call_term(name, len(arguments))
        frame = Frame()
        for i, v in enumerate(arguments):
            frame[i] = v
        rr = simplify(t, frame)
        if rr == Terminal(0):
            return None
        if rr == Terminal(1):
            return ret_variable.getValue(frame)
        assert False  # something else represented as the R-expr

    def lookup_term(self, name, *, ignore=()):
        # if a term isn't defined, we are going to return Terminal(0) as there
        # is nothing that could have unified with the given expression.  we do
        # included tracking with the assumption, so if it later defined, we are
        # able to change the expression.

        if isinstance(name, tuple) and len(name) == 2 and name[0] == '$':
            # make is so that $(1,2,3,4,5) can be used as a tuple type, regardless of the size
            return BuildStructure('$', ret_variable, tuple(VariableId(i) for i in range(name[1])))
        elif name in self.terms_as_memoized and 'memo' not in ignore:
            # do not include additional assumptions as the assumptions are "broken" by the memo table
            # which includes its own assumptions
            return self.terms_as_memoized[name]  # should contain an RMemo type which will perform reads from a memo table
        elif name in self.terms_as_compiled and 'compile' not in ignore:
            # return a wrapper around the compiled expression such that it can
            # be embedded into the R-expr by the interpreter
            ct = self.terms_as_compiled[name]
            r = EnterCompiledCode(ct, ct.variable_order)
        elif name in self.terms_as_optimized and 'optimized' not in ignore:
            r = self.terms_as_optimized[name]  # this is the term rewritten after having been passed through the optimizer
        elif name in self.terms_as_defined:
            r = self.terms_as_defined[name]  # something that was defined directly by the user
        elif isinstance(name, MergedExpression) and name in self.merged_expressions:
            r = name.expression  # the optimizer has combined some states together, but we have not processed this item yet, so we can just return the expression as it will be semantically equivalent
        elif self.parent:
            r = self.parent.lookup_term(name, ignore=ignore)
        else:
            assert not isinstance(name, (MergedExpression,CompiledExpression))  # this should get handled at some point along the chain. So it should not get to this undefiend point
            r = Terminal(0)  # this should probably be an error or something so that we can identify that this method doesn't exit
            if 'not_found' not in ignore:
                print('[warn] failed lookup', name)

        if 'assumption' not in ignore:
            # this assumption tracks if the code changes at all and the result
            # should be refreshed this can included new compiled versions as
            # well as new results values as a result of memoization
            a = self.term_assumption(name)
            assert a.isValid()
            r = AssumptionWrapper(a, r)
        elif 'assumption_defined' not in ignore:
            # this assumption is only invalidated in the case that a term is
            # changed by the user at the repl or by an external driver program.
            a = self.term_as_defined_assumption(name)
            assert a.isValid()
            r = AssumptionWrapper(a, r)

        return r

    def run_agenda(self):
        return self.agenda.run()

    def optimize_system(self):
        # want to optimize all of the rules in the program, which will then
        # require that expressions are handled if they are later invalidated?

        for term, rexpr in self.terms_as_defined.items():
            # this should call optimize on everything?

            b = check_basecases(rexpr)
            if b != 3:
                # then we want to try and improve this expression as there is
                # something that we can try and optimize.
                self.optimize_term(term)

    def create_merged_expression(self, expr :RBaseType, exposed_vars: Set[Variable]):
        # if there are some terms that are combiend, then we want to be made
        # aware of that, so that we can plan optimizations on the new inferred
        # terms.

        r = MergedExpression(expr, exposed_vars)

        if r in self.merged_expressions:
            r2 = self.merged_expressions[r]
            # then we want to check that the exposed variables are the same, or
            # mark that there are more exposed variables.
            assert r2.exposed_vars == exposed_vars  # TODO: handle != case
            r = r2
        else:
            self.merged_expressions[r] = r
            self.agenda.push(lambda: self._optimize_term(r))  # need to processes this new thing and try the optimizer at it
        return r

    def create_compiled_expression(self, term_ref, exposed_vars: Set[Variable]):
        if term_ref in self.terms_as_compiled:
            r = self.terms_as_compiled[term_ref]
            assert set(r.exposed_vars) == set(exposed_vars)
        else:
            r = CompiledExpression(term_ref, exposed_vars)
            self.terms_as_compiled[term_ref] = r
        return r

    def optimize_term(self, term):
        self.agenda.push(lambda: self._optimize_term(term))

    def _optimize_term(self, term):
        popt = self.terms_as_optimized.get(term)  # get the current optimized version of the code
        assumpt = self.term_assumption(term)
        assumpt_d = self.term_as_defined_assumption(term)
        if isinstance(term, MergedExpression):
            # then we are going to determine which variables are
            r = term.expression
            exposed = term.exposed_vars
        else:
            name, arity = term  # the name matching the way that we are storing dyna terms
            r = self.terms_as_defined[term]
            exposed = (ret_variable, *variables_named(*range(arity)))
        rr, assumptions = run_optimizer(r, exposed)

        assumptions.add(assumpt_d)
        #assumptions.add(assumpt)
        assumptions.discard(assumpt)  # don't want this assumption contained as it could cause a cycle
        #assert assumpt not in assumptions

        bc = check_basecases(rr, stack=(term,))
        if bc == 0:
            # then there is no way for this to ever match something, so just report it as empty
            rr = Terminal(0)

        # if the assumption used for optimizing is invalidated, then push work to the agenda to
        # redo the optimization
        assumption_response = AssumptionResponse(lambda: self.agenda.push(lambda: self._optimize_term(term)))
        #assumption_response = AssumptionResponse(lambda: 1/0) #self.agenda.push(lambda: self._optimize_term(term)))

        invalidate = False

        if popt is None or not rr.possibly_equal(popt):
            if rr.isEmpty() or popt is not None:
                # then we have "proven" something interesting, so we are going
                # to use the assumption to notify anything that might want to
                # read from this expression.
                invalidate = True
            elif self.term_assumption(('$reflect', 3)) in assumptions or self.term_assumption(('$reflect', 4)) in assumptions:
                # if we have managed to remove the reflection operation from the code, then we should also invalidate
                # as something else might benifit from this
                for child in rr.all_children():
                    if isinstance(child, CallTerm) and (child.term_ref == ('$reflect', 3) or child.term_ref == ('$reflect', 4)):
                        break
                else:
                    invalidate = True

            self.terms_as_optimized[term] = rr

        if invalidate:
            assert all(a.isValid() for a in assumptions)
            #print(assumptions)
            self.invalidate_term_assumption(term)
            #print('post', assumptions)

        for a in assumptions:
            a.track(assumption_response)

    def _compile_term(self, term, ground_vars :Set[Variable]):
        # always use the lookup as this can get optimized versions
        R = self.lookup_term(term, ignore=('compile', 'memo'))
        if isinstance(term, MergedExpression):
            exposed = term.exposed_vars
        else:
            # for dyna expressions the exposed public variables always have the same names
            name, arity = term
            exposed = (ret_variable, *variables_named(*range(arity)))

        ce = self.create_compiled_expression(term, exposed)
        incoming_mode = tuple(v in ground_vars for v in ce.variable_order)  # the mode about which variables are ground at the start

        result = run_compiler(self, ce, R, incoming_mode)

        #raise NotImplemented()

    def watch_term_changes(self, term, callback):
        # in the case that a term changes value, then this can get a callback
        # this should just be a null epression which watches the term?  And then
        # would notify the callback in the case that something changes

        name, arity = term
        variables = variables_named(*range(arity))+(ret_variable,)

        class AL(Assumption):
            def signal(self, msg):
                callback(msg)

        R = self.call_term(name, arity)
        R = partition(variables, [R])
        argument_mode = (True,)*arity+(False,)
        supported_mode = (False,)*len(argument_mode)
        memos = MemoContainer(argument_mode, supported_mode, variables, R, is_null_memo=True, assumption_always_listen=(AL(),), dyna_system=self)

        return memos

    def add_rules(self, string):
        from dyna.syntax.normalizer import add_rules
        add_rules(string, self)


# where we will define the builtins etc the base dyna base, for now there will
# just be a single one of these that is global however we should not use the
# global reference whenever possible, as will want to turn this into the
# dynabase references
#dyna_system = SystemContext()



class MergedExpression:
    """Used for merginging multiple terms and primitive operations and then storing
    then in the context.  This is created by the optimizers.
    """

    # ??? this seems like the wrong file for this expression.  maybe this file
    # should instead be focused on being the system based definition.  so the
    # pointers to different operations, and then being able to combine a term is
    # just something that requires a new name, and thus we use this

    expression : RBaseType
    exposed_vars : Set[Variable]

    def __init__(self, expression, exposed_vars):
        self.expression = expression
        self.exposed_vars = exposed_vars

    def __eq__(self, other):
        return type(self) is type(other) and self.expression == other.expression

    def __hash__(self):
        return hash(type(self)) ^ hash(self.expression)

    def __repr__(self):
        return f'MergedExpression({self.expression})'


class CompiledExpression:

    term_ref : object  # references the term that we are compiling
    exposed_vars : Set[Variable]  # the variables that the public API
    variable_order : Tuple[Variable]  # some order such that we can easily lookup compiled moded expressions
    compiled_expressions : Dict[Tuple[bool], object]  # map from compiled modes in the variable_order to resuling compiled expressions

    def __init__(self, term_ref, exposed_vars :Set[Variable]):
        self.term_ref = term_ref
        self.exposed_vars = exposed_vars
        # there should not be any constant variables in the exposed set as we
        # won't be able to change this as parameters
        assert all(not isinstance(v, ConstantVariable) for v in exposed_vars)
        self.variable_order = tuple(exposed_vars)
        self.compiled_expressions = {}

    def __eq__(self, other):
        return type(self) is type(other) and self.term_ref == other.term_ref and self.exposed_vars == other.exposed_vars

    def __hash__(self):
        return hash(type(self)) ^ hash(self.term_ref) ^ reduce(operator.xor, map(hash, self.exposed_vars), 0)



def check_basecases(R, stack=()):
    # check that this expression can hit some basecase, otherwise this
    # expression must just be terminal zero.  This is needed to get the tree set
    # stuff to work....(as it is sorta the emptyness test)

    # this should be that there is always some branch of a partition that would
    # not encounter something that is on the stack.  If something always
    # encounters whatever is on the stack, then it would always just keep
    # running forever.  So what we are looking for is basically a branch of a
    # partition whcih can avoid calling back to itself.

    # return:
    #   0 then it does not hit a basecase for sure, and we should report an error or just mark this as terminal(0) as it would never be able to terminate
    #   1 unsure as it uses evaluate on all branches so it could go anywhere
    #   2 this is definitly uses indirectly or directly recursion but this thing has base cases
    #   3 there is no detected recursion

    if isinstance(R, (Evaluate, Evaluate_reflect)):
        return 1  # unsure in this case, we could go /anywhere/
    elif isinstance(R, Partition):
        # partition, so highest score, and only 3 if all branches return 3
        x = 0; z = 3
        for r in R.children:
            y = check_basecases(r, stack=stack)
            if y > x: x = y  # x is the max
            if y < z: z = y  # z is the min
        if z == 3:
            return 3  # no recursion detected on any branch
        return min(x, 2)  # some recursion

    elif isinstance(R, CallTerm):
        if R.term_ref in stack:
            return 0  # hit ourselves in a recursive attempt
        else:
            return check_basecases(R.dyna_system.lookup_term(R.term_ref), stack=stack+(R.term_ref,))
    else:
        # assume intersection, so the lowest score is returned
        x = 3
        for r in R.children:
            y = check_basecases(r, stack=stack)
            if x > y: x = y
            if x == 0: return x
        return x


# class TaskContext:
#     """
#     Represent the context for a given runtime transaction.
#     This is going to be some task that pops of the agenda.
#     The reason for this class is basically we might want to mask some memo table with new updates,
#     or track what operations would need to be pushed to the agenda.
#     The infinite priority agenda should basically be "pushing" something to this local agenda,
#     but then choosing to run it /instantly/ at this point, and then getting the result instead of using whatever the /guessed/ was going to be
#     """
#     def __init__(self, system):
#         self.new_memos = {}
#         self.agenda_additions = []
#         self.system = system



# # this should be a thread local.  The reference to the dyna_system should probably go through this also?
# active_task = TaskContext(dyna_system)
