from typing import *
from collections import deque

class Agenda:

    def __init__(self):
        self._agenda = deque()
        self._contains = set()
        self._agenda_empty_notfies = []  # list of Callable

    def push(self, task: Callable):
        # first check if the work is already added to the agenda.  In which case this should not be processed
        if task not in self._contains:
            self._agenda.append(task)
            self._contains.add(task)

    def pop(self):
        if self._agenda:
            r = self._agenda.popleft()
            self._contains.remove(r)
            return r

    def run(self):
        while self._agenda:
            while self._agenda:
                r = self.pop()
                #print(r)
                r()  # run the task.
            # when the agenda is drained, then we want to notify these other systems
            # these might push more agenda operations, which is why we loop around again
            for n in self._agenda_empty_notfies:
                n()

    def __bool__(self):
        return bool(self._agenda)


class AgendaWork(Callable):
    __slots__ = ('func', 'work')
    def __init__(self, func, work):
        self.func = func
        self.work = work
    def __call__(self):
        self.func(self.work)
    def __hash__(self):
        return hash(self.func) ^ hash(self.work)
    def __eq__(self, other):
        return isinstance(other, AgendaWork) and self.func == other.func and self.work == other.work
    def __str__(self):
        return f'{self.func}({self.work})'
    __repr__ = __str__


def push_work(func, work, dyna_system=None):
    # I suppose that there should be some "global" accessable function which can
    # do the agenda pushes, which is either going to be pushing to some local
    # task context or directly to the system's agenda?
    #import ipdb; ipdb.set_trace()
    if dyna_system is None:
        from . import dyna_system
    dyna_system.agenda.push(AgendaWork(func, work))
