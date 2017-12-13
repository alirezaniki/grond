from __future__ import print_function
import numpy as num
import math
import copy
import logging
import os.path as op
import os
import time

from pyrocko import gf, util, guts
from pyrocko.guts import Object, String, Bool, List, Dict, Int

from ..meta import ADict, Parameter, GrondError, xjoin
from ..targets import WaveformMisfitTarget, SatelliteMisfitTarget


guts_prefix = 'grond'
logger = logging.getLogger('grond')
km = 1e3
as_km = dict(scale_factor=km, scale_unit='km')


def nextpow2(i):
    return 2**int(math.ceil(math.log(i)/math.log(2.)))


class ProblemConfig(Object):
    name_template = String.T()
    apply_balancing_weights = Bool.T(default=True)
    norm_exponent = Int.T(default=2)


class Problem(Object):
    name = String.T()
    ranges = Dict.T(String.T(), gf.Range.T())
    dependants = List.T(Parameter.T())
    apply_balancing_weights = Bool.T(default=True)
    norm_exponent = Int.T(default=2)
    base_source = gf.Source.T(optional=True)

    targets = List.T()

    def __init__(self, **kwargs):
        Object.__init__(self, **kwargs)

        self._target_weights = None
        self._engine = None
        self._family_mask = None

        if hasattr(self, 'problem_waveform_parameters') and self.has_waveforms:
            self.problem_parameters =\
                self.problem_parameters + self.problem_waveform_parameters

        logger.name = self.__class__.__name__

    def get_engine(self):
        return self._engine

    def copy(self):
        o = copy.copy(self)
        o._target_weights = None
        return o

    def set_target_parameter_values(self, x):
        i = len(self.problem_parameters)
        for target in self.targets:
            n = len(target.target_parameters)
            target.set_parameter_values(x[i:i+n])
            i += n

    def get_parameter_dict(self, model, group=None):
        params = []
        for ip, p in enumerate(self.parameters):
            if group in p.groups:
                params.append((p.name, model[ip]))
        return ADict(params)

    def get_parameter_array(self, d):
        arr = num.zeros(self.nparameters, dtype=num.float)
        for ip, p in enumerate(self.parameters):
            if p.name in d.keys():
                arr[ip] = d[p.name]
        return arr

    def dump_problem_info(self, dirname):
        fn = op.join(dirname, 'problem.yaml')
        util.ensuredirs(fn)
        guts.dump(self, filename=fn)

    def dump_problem_data(
            self, dirname, x, misfits,
            accept=None, ibootstrap_choice=None, ibase=None):

        fn = op.join(dirname, 'models')
        if not isinstance(x, num.ndarray):
            x = num.array(x)
        with open(fn, 'ab') as f:
            x.astype('<f8').tofile(f)

        fn = op.join(dirname, 'misfits')
        with open(fn, 'ab') as f:
            misfits.astype('<f8').tofile(f)

        if None not in (ibootstrap_choice, ibase):
            fn = op.join(dirname, 'choices')
            with open(fn, 'ab') as f:
                num.array((ibootstrap_choice, ibase), dtype='<i8').tofile(f)

        if accept is not None:
            fn = op.join(dirname, 'accepted')
            with open(fn, 'ab') as f:
                accept.astype('<i1').tofile(f)

    def name_to_index(self, name):
        pnames = [p.name for p in self.combined]
        return pnames.index(name)

    @property
    def parameters(self):
        target_parameters = []
        for target in self.targets:
            target_parameters.extend(target.target_parameters)
        return self.problem_parameters + target_parameters

    @property
    def parameter_names(self):
        return [p.name for p in self.combined]

    @property
    def nparameters(self):
        return len(self.parameters)

    @property
    def ntargets(self):
        return len(self.targets)

    @property
    def ntargets_waveform(self):
        return len(self.waveform_targets)

    @property
    def ntargets_static(self):
        return len(self.satellite_targets)

    @property
    def nmisfits(self):
        nmisfits = 0
        for target in self.targets:
            nmisfits += target.nmisfits
        return nmisfits

    @property
    def ndependants(self):
        return len(self.dependants)

    @property
    def ncombined(self):
        return len(self.parameters) + len(self.dependants)

    @property
    def combined(self):
        return self.parameters + self.dependants

    @property
    def satellite_targets(self):
        return [t for t in self.targets
                if isinstance(t, SatelliteMisfitTarget)]

    @property
    def waveform_targets(self):
        return [t for t in self.targets
                if isinstance(t, WaveformMisfitTarget)]

    @property
    def has_statics(self):
        if self.satellite_targets:
            return True
        return False

    @property
    def has_waveforms(self):
        if self.waveform_targets:
            return True
        return False

    def set_engine(self, engine):
        self._engine = engine

    def random_uniform(self, xbounds):
        x = []
        for i in xrange(self.nparameters):
            x.append(num.random.uniform(xbounds[i, 0], xbounds[i, 1]))

        return num.array(x, dtype=num.float)

    def preconstrain(self, x):
        return x

    def extract(self, xs, i):
        if xs.ndim == 1:
            return self.extract(xs[num.newaxis, :], i)[0]

        if i < self.nparameters:
            return xs[:, i]
        else:
            return self.make_dependant(
                xs, self.dependants[i-self.nparameters].name)

    def get_target_weights(self):
        if self._target_weights is None:
            self._target_weights = num.array(
                [target.get_combined_weight(
                    apply_balancing_weights=self.apply_balancing_weights)
                 for target in self.targets], dtype=num.float)

        return self._target_weights

    def inter_family_weights(self, ns):
        exp, root = self.get_norm_functions()

        family, nfamilies = self.get_family_mask()

        ws = num.zeros(self.ntargets)
        for ifamily in range(nfamilies):
            mask = family == ifamily
            ws[mask] = 1.0 / root(num.nansum(exp(ns[mask])))

        return ws

    def inter_family_weights2(self, ns):
        '''
        :param ns: 2D array with normalization factors ``ns[imodel, itarget]``
        :returns: 2D array ``weights[imodel, itarget]``
        '''

        exp, root = self.get_norm_functions()

        family, nfamilies = self.get_family_mask()
        ws = num.zeros(ns.shape)
        for ifamily in range(nfamilies):
            mask = family == ifamily
            ws[:, mask] = (1.0 / root(
                num.nansum(exp(ns[:, mask]), axis=1)))[:, num.newaxis]

        return ws

    def get_xref(self):
        return self.pack(self.base_source)

    def get_parameter_bounds(self):
        out = []
        for p in self.problem_parameters:
            r = self.ranges[p.name]
            out.append((r.start, r.stop))

        for target in self.targets:
            for p in target.target_parameters:
                r = target.target_ranges[p.name]
                out.append((r.start, r.stop))

        return num.array(out, dtype=num.float)

    def get_dependant_bounds(self):
        return num.zeros((0, 2))

    def get_combined_bounds(self):
        return num.vstack((
            self.get_parameter_bounds(),
            self.get_dependant_bounds()))

    def raise_invalid_norm_exponent(self):
        raise GrondError('invalid norm exponent' % self.norm_exponent)

    def get_norm_functions(self):
        if self.norm_exponent == 2:
            def sqr(x):
                return x**2

            return sqr, num.sqrt

        elif self.norm_exponent == 1:
            def noop(x):
                return x

            return noop, num.abs

        else:
            self.raise_invalid_norm_exponent()

    def combine_misfits(
            self, misfits,
            extra_weights=None,
            get_contributions=False):

        exp, root = self.get_norm_functions()

        if misfits.ndim == 2:
            misfits = misfits[num.newaxis, :, :]
            return self.combine_misfits(misfits, extra_weights)[0, ...]

        assert misfits.ndim == 3
        assert extra_weights is None or extra_weights.ndim == 2

        if extra_weights is not None:
            w = extra_weights[num.newaxis, :, :] \
                * self.get_target_weights()[num.newaxis, num.newaxis, :] \
                * self.inter_family_weights2(
                    misfits[:, :, 1])[:, num.newaxis, :]

            if get_contributions:
                return exp(w*misfits[:, num.newaxis, :, 0]) \
                    / num.nansum(exp(w*misfits[:, num.newaxis, :, 1]), axis=2)

            return root(
                num.nansum(exp(w*misfits[:, num.newaxis, :, 0]), axis=2) /
                num.nansum(exp(w*misfits[:, num.newaxis, :, 1]), axis=2))
        else:
            w = self.get_target_weights()[num.newaxis, :] \
                * self.inter_family_weights2(misfits[:, :, 1])

            if get_contributions:
                return exp(w*misfits[:, :, 0]) \
                    / num.nansum(exp(w*misfits[:, :, 1]), axis=1)

            return root(
                num.nansum(exp(w*misfits[:, :, 0]), axis=1) /
                num.nansum(exp(w*misfits[:, :, 1]), axis=1))

    def make_family_mask(self):
        family_names = set()
        families = num.zeros(len(self.targets), dtype=num.int)

        for itarget, target in enumerate(self.targets):
            family_names.add(target.normalisation_family)
            families[itarget] = len(family_names) - 1

        return families, len(family_names)

    def get_family_mask(self):
        if self._family_mask is None:
            self._family_mask = self.make_family_mask()

        return self._family_mask

    def evaluate(self, x, dump_data=False):
        raise NotImplementedError()

    def forward(self, x):
        source = self.get_source(x)
        engine = self.get_engine()
        plain_targets = [target.get_plain_target() for target in self.targets]

        resp = engine.process(source, plain_targets)
        results = []
        for target, result in zip(self.targets, resp.results_list[0]):
            if isinstance(result, gf.SeismosizerError):
                logger.debug(
                    '%s.%s.%s.%s: %s' % (target.codes + (str(result),)))

                results.append(None)
            else:
                results.append(result)

        return results


class InvalidRundir(Exception):
    pass


class ModelHistory(object):
    nmodels_capacity_min = 1024

    def __init__(self, problem, path=None, mode='r'):
        '''Model History lets you write, read and follow new models

        :param problem: The Problem
        :type problem: :class:`grond.Problem`
        :param path: Rundir to use, defaults to None
        :type path: str, optional
        :param mode: Mode to use, defaults to 'r'.
            'r': Read, 'w': Write
        :type mode: str, optional
        '''
        self.problem = problem
        self.path = path
        self._models_buffer = None
        self._misfits_buffer = None
        self.models = None
        self.misfits = None
        self.nmodels_capacity = self.nmodels_capacity_min
        self.listeners = []
        self.mode = mode

        if mode == 'r':
            self.verify_rundir(self.path)
            models, misfits = load_problem_data(path, problem)
            self.extend(models, misfits)

    @staticmethod
    def verify_rundir(rundir):
        _rundir_files = ['misfits', 'models']

        if not op.exists(rundir):
            raise OSError('Rundir %s does not exist!' % rundir)
        for f in _rundir_files:
            if not op.exists(op.join(rundir, f)):
                raise InvalidRundir('File %s not found!' % f)

    @classmethod
    def follow(cls, path, wait=20.):
        '''Start following a rundir

        :param path: The path to follow, a grond rundir
        :type path: str, optional
        :param wait: Wait time until the folder become alive, defaults to 10.
        :type wait: number in seconds, optional
        :returns: A ModelHistory instance
        :rtype: :class:`grond.core.ModelHistory`
        '''
        start_watch = time.time()
        while (time.time() - start_watch) < wait:
            try:
                cls.verify_rundir(path)
                problem = load_problem_info(path)
                return cls(problem, path, mode='r')
            except InvalidRundir:
                time.sleep(.25)

    @property
    def nmodels(self):
        if self.models is None:
            return 0
        else:
            return self.models.shape[0]

    @nmodels.setter
    def nmodels(self, nmodels_new):
        assert 0 <= nmodels_new <= self.nmodels
        self.models = self._models_buffer[:nmodels_new, :]
        self.misfits = self._misfits_buffer[:nmodels_new, :, :]

    @property
    def nmodels_capacity(self):
        if self._models_buffer is None:
            return 0
        else:
            return self._models_buffer.shape[0]

    @nmodels_capacity.setter
    def nmodels_capacity(self, nmodels_capacity_new):
        if self.nmodels_capacity != nmodels_capacity_new:
            models_buffer = num.zeros(
                (nmodels_capacity_new, self.problem.nparameters),
                dtype=num.float)

            misfits_buffer = num.zeros(
                (nmodels_capacity_new, self.problem.ntargets, 2),
                dtype=num.float)

            ncopy = min(self.nmodels, nmodels_capacity_new)

            if self._models_buffer is not None:
                models_buffer[:ncopy, :] = \
                    self._models_buffer[:ncopy, :]
                misfits_buffer[:ncopy, :, :] = \
                    self._misfits_buffer[:ncopy, :, :]

            self._models_buffer = models_buffer
            self._misfits_buffer = misfits_buffer

    def clear(self):
        self.nmodels = 0
        self.nmodels_capacity = self.nmodels_capacity_min

    def extend(self, models, misfits):
        nmodels = self.nmodels

        n = models.shape[0]

        nmodels_capacity_want = max(
            self.nmodels_capacity_min, nextpow2(nmodels + n))

        if nmodels_capacity_want != self.nmodels_capacity:
            self.nmodels_capacity = nmodels_capacity_want

        self._models_buffer[nmodels:nmodels+n, :] = models
        self._misfits_buffer[nmodels:nmodels+n, :, :] = misfits

        self.models = self._models_buffer[:nmodels+n, :]
        self.misfits = self._misfits_buffer[:nmodels+n, :, :]

        if self.path and self.mode == 'w':
            for i in range(n):
                self.problem.dump_problem_data(
                        self.path, models[i, :], misfits[i, :, :])

        self.emit('extend', nmodels, n, models, misfits)

    def append(self, model, misfits):
        nmodels = self.nmodels

        nmodels_capacity_want = max(
            self.nmodels_capacity_min, nextpow2(nmodels + 1))

        if nmodels_capacity_want != self.nmodels_capacity:
            self.nmodels_capacity = nmodels_capacity_want

        self._models_buffer[nmodels, :] = model
        self._misfits_buffer[nmodels, :, :] = misfits

        self.models = self._models_buffer[:nmodels+1, :]
        self.misfits = self._misfits_buffer[:nmodels+1, :, :]

        if self.path and self.mode == 'w':
            self.problem.dump_problem_data(
                self.path, model, misfits)

        self.emit(
            'extend', nmodels, 1,
            model[num.newaxis, :], misfits[num.newaxis, :, :])

    def update(self):
        ''' Update history from path '''
        nmodels_available = get_nmodels(self.path, self.problem)
        if self.nmodels == nmodels_available:
            return
        new_models, new_misfits = load_problem_data(
            self.path, self.problem, skip_models=self.nmodels)

        self.extend(new_models, new_misfits)

    def add_listener(self, listener):
        self.listeners.append(listener)

    def emit(self, event_name, *args, **kwargs):
        for listener in self.listeners:
            slot = getattr(listener, event_name, None)
            if callable(slot):
                slot(*args, **kwargs)


def get_nmodels(dirname, problem):
    fn = op.join(dirname, 'models')
    with open(fn, 'r') as f:
        return os.fstat(f.fileno()).st_size // (problem.nparameters * 8)


def load_problem_info_and_data(dirname, subset=None):
    problem = load_problem_info(dirname)
    models, misfits = load_problem_data(xjoin(dirname, subset), problem)
    return problem, models, misfits


def load_optimizer_info(dirname):
    fn = op.join(dirname, 'optimizer.yaml')
    return guts.load(filename=fn)


def load_problem_info(dirname):
    fn = op.join(dirname, 'problem.yaml')
    return guts.load(filename=fn)


def load_problem_data(dirname, problem, skip_models=None):
    if skip_models is None:
        skip_models = 0
    nmodels = get_nmodels(dirname, problem)

    fn = op.join(dirname, 'models')
    with open(fn, 'r') as f:
        nmodels = os.fstat(f.fileno()).st_size // (problem.nparameters * 8)
        nmodels -= skip_models
        f.seek(skip_models * problem.nparameters * 8)
        models = num.fromfile(
                f, dtype='<f8',
                count=nmodels * problem.nparameters)\
            .astype(num.float)

    nmodels = models.size // problem.nparameters
    models = models.reshape((nmodels, problem.nparameters))

    fn = op.join(dirname, 'misfits')
    with open(fn, 'r') as f:
        f.seek(skip_models * problem.ntargets * 2 * 8)
        misfits = num.fromfile(
                f, dtype='<f8',
                count=nmodels*problem.ntargets*2)\
            .astype(num.float)

    misfits = misfits.reshape((nmodels, problem.ntargets, 2))

    return models, misfits


__all__ = '''
    Problem
    ModelHistory
    ProblemConfig
    load_problem_info
    load_problem_info_and_data
'''.split()
