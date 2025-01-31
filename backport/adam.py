"""
  This file is backported from: https://github.com/chainer/chainer/blob/80430c8a21b3baf7df9b899107272cab876cc738/chainer/optimizers/adam.py
  Remove it when merged to official stable repository
"""

from __future__ import division
import math
import warnings

import numpy

from chainer import backend
from chainer.backends import cuda
from chainer.backends import intel64
from chainer import optimizer
#from chainer import types

#if types.TYPE_CHECKING:
#    import typing_extensions as tpe
#
#    class AdamHyperparameter(tpe.Protocol):
#        """Protocol class for hyperparameter of Adam.
#        This is only for PEP 544 compliant static type checkers.
#        """
#        alpha = None  # type: float
#        beta1 = None  # type: float
#        beta2 = None  # type: float
#        eps = None  # type: float
#        eta = None  # type: float
#        weight_decay_rate = None  # type: float
#        amsgrad = None  # type: bool
#        adabound = None  # type: bool
#        final_lr_rate = None  # type: float
#        gamma = None  # type: float

_default_hyperparam = optimizer.Hyperparameter()  # type: AdamHyperparameter # NOQA
_default_hyperparam.alpha = 0.001
_default_hyperparam.beta1 = 0.9
_default_hyperparam.beta2 = 0.999
_default_hyperparam.eps = 1e-8
_default_hyperparam.eta = 1.0
_default_hyperparam.weight_decay_rate = 0
_default_hyperparam.amsgrad = False
_default_hyperparam.adabound = False
_default_hyperparam.final_lr_rate = 100.0
_default_hyperparam.gamma = 1e-3


def _learning_rate(hp, t):
    if t == 0:
        raise RuntimeError(
            'Can\'t determine the learning rate of Adam optimizer '
            'because the update steps have not been started.')
    fix1 = 1. - math.pow(hp.beta1, t)
    fix2 = 1. - math.pow(hp.beta2, t)
    return hp.alpha * math.sqrt(fix2) / fix1


class AdamRule(optimizer.UpdateRule):

    """Update rule of Adam optimization algorithm.
    See: `Adam: A Method for Stochastic Optimization \
          <https://arxiv.org/abs/1412.6980v8>`_
    Modified for proper weight decay.
    See: `Fixing Weight Decay Regularization in Adam \
          <https://openreview.net/forum?id=rk6qdGgCZ>`_
    With option to use AMSGrad variant of Adam.
    See: `On the Convergence of Adam and Beyond \
          <https://openreview.net/forum?id=ryQu7f-RZ>`_
    With option to use AdaBound variant of Adam.
    See: `Adaptive Gradient Methods with Dynamic Bound of Learning Rate \
          <https://openreview.net/forum?id=Bkg3g2R9FX>`
    See :class:`~chainer.optimizers.Adam` for the default values
    of the hyperparameters.
    Args:
        parent_hyperparam (~chainer.optimizer.Hyperparameter): Hyperparameter
            that provides the default values.
        alpha (float): Coefficient of learning rate.
        beta1 (float): Exponential decay rate of the first order moment.
        beta2 (float): Exponential decay rate of the second order moment.
        eps (float): Small value for the numerical stability.
        eta (float): Schedule multiplier, can be used for warm restarts.
        weight_decay_rate (float): Weight decay rate.
        amsgrad (bool): Whether to use the AMSGrad variant of Adam.
        adabound (bool): Whether to use the AdaBound variant of Adam.
        final_lr_rate (float): final (SGD) learning rate per base-alpha in adabound.
        gamma (float): convergence speed of the bound functions in adabound.
    """
    _kernel = None
    _amsgrad_kernel = None
    _adabound_kernel = None
    _amsbound_kernel = None

    def __init__(self, parent_hyperparam=None,
                 alpha=None, beta1=None, beta2=None, eps=None,
                 eta=None, weight_decay_rate=None, amsgrad=None,
                 adabound=None, final_lr_rate=None, gamma=None):
        super(AdamRule, self).__init__(
            parent_hyperparam or _default_hyperparam)
        if alpha is not None:
            self.hyperparam.alpha = alpha
        if beta1 is not None:
            self.hyperparam.beta1 = beta1
        if beta2 is not None:
            self.hyperparam.beta2 = beta2
        if eps is not None:
            self.hyperparam.eps = eps
        if eta is not None:
            self.hyperparam.eta = eta
        if weight_decay_rate is not None:
            self.hyperparam.weight_decay_rate = weight_decay_rate
        if amsgrad is not None:
            self.hyperparam.amsgrad = amsgrad
        if adabound is not None:
            self.hyperparam.adabound = adabound
        if final_lr_rate is not None:
            self.hyperparam.final_lr_rate = final_lr_rate
        if gamma is not None:
            self.hyperparam.gamma = gamma

    def init_state(self, param):
        xp = backend.get_array_module(param.data)
        with cuda.get_device_from_array(param.data):
            self.state['m'] = xp.zeros_like(param.data)
            self.state['v'] = xp.zeros_like(param.data)
            if self.hyperparam.amsgrad:
                self.state['vhat'] = xp.zeros_like(param.data)

        # For iDeep
        if isinstance(param.data, intel64.mdarray):
            self.state['m'] = intel64.ideep.array(
                self.state['m'], itype=intel64.ideep.wgt_array)
            self.state['v'] = intel64.ideep.array(
                self.state['v'], itype=intel64.ideep.wgt_array)

    def update_core_cpu(self, param):
        grad = param.grad
        if grad is None:
            return
        hp = self.hyperparam
        eps = grad.dtype.type(hp.eps)
        if hp.eps != 0 and eps == 0:
            raise ValueError(
                'eps of Adam optimizer is too small for {} ({})'.format(
                    grad.dtype.name, hp.eps))
        m, v = self.state['m'], self.state['v']
        if (isinstance(m, intel64.mdarray)
                and isinstance(v, intel64.mdarray)):
            m.inplace_axpby(1.0, 1.0 - hp.beta1, grad - m)
            v.inplace_axpby(1.0, 1.0 - hp.beta2, grad*grad - v)
            if hp.amsgrad:
                vhat = self.state['vhat']
                numpy.maximum(vhat, v, out=vhat)
            else:
                vhat = v
            step = self.alpha_t / (numpy.sqrt(vhat) + hp.eps)
            if hp.adabound:
                lower, upper = self.bounds
                step = numpy.clip(step, lower, upper)
            param.data.inplace_axpby(
                1.0 - hp.weight_decay_rate, -hp.eta, m * step)
        else:
            m += (1 - hp.beta1) * (grad - m)
            v += (1 - hp.beta2) * (grad * grad - v)
            if hp.amsgrad:
                vhat = self.state['vhat']
                numpy.maximum(vhat, v, out=vhat)
            else:
                vhat = v
            step = self.alpha_t / (numpy.sqrt(vhat) + hp.eps)
            if hp.adabound:
                lower, upper = self.bounds
                step = numpy.clip(step, lower, upper)
            param.data -= hp.eta * (
                m * step + hp.weight_decay_rate * param.data)
        #from lpu.common import logging
        #logger = logging.getColorLogger(__name__)
        #logger.debug_print(lower)
        #logger.debug_print(upper)
        #logger.debug_print(step)

    def update_core_gpu(self, param):
        grad = param.grad
        if grad is None:
            return

        hp = self.hyperparam
        eps = grad.dtype.type(hp.eps)
        if hp.eps != 0 and eps == 0:
            raise ValueError(
                'eps of Adam optimizer is too small for {} ({})'.format(
                    grad.dtype.name, hp.eps))
        if hp.adabound:
            lower, upper = self.bounds
        if hp.amsgrad and hp.adabound:
            if AdamRule._amsbound_kernel is None:
                AdamRule._amsbound_kernel = cuda.elementwise(
                    'T grad, T alpha_t, T one_minus_beta1, T one_minus_beta2, '
                    'T lower, T upper, '
                    'T eps, T eta, T weight_decay_rate',
                    'T param, T m, T v, T vhat',
                    '''m += one_minus_beta1 * (grad - m);
                       v += one_minus_beta2 * (grad * grad - v);
                       vhat = max(vhat, v);
                       param -= eta * (m * max(lower, min(upper, alpha_t / (sqrt(vhat) + eps))) +
                                       weight_decay_rate * param);''',
                    'amsbound')
            AdamRule._amsbound_kernel(
                grad, self.alpha_t, 1 - hp.beta1,
                1 - hp.beta2, lower, upper, hp.eps,
                hp.eta, hp.weight_decay_rate,
                param.data, self.state['m'], self.state['v'],
                self.state['vhat'])
        elif hp.adabound:
            if AdamRule._adabound_kernel is None:
                AdamRule._adabound_kernel = cuda.elementwise(
                    'T grad, T alpha_t, T one_minus_beta1, T one_minus_beta2, '
                    'T lower, T upper, '
                    'T eps, T eta, T weight_decay_rate',
                    'T param, T m, T v',
                    '''m += one_minus_beta1 * (grad - m);
                       v += one_minus_beta2 * (grad * grad - v);
                       param -= eta * (m * max(lower, min(upper, alpha_t / (sqrt(v) + eps))) +
                                       weight_decay_rate * param);''',
                    'adabound')
            AdamRule._adabound_kernel(
                grad, self.alpha_t, 1 - hp.beta1,
                1 - hp.beta2, lower, upper, hp.eps,
                hp.eta, hp.weight_decay_rate,
                param.data, self.state['m'], self.state['v'])
        elif hp.amsgrad:
            if AdamRule._amsgrad_kernel is None:
                AdamRule._amsgrad_kernel = cuda.elementwise(
                    'T grad, T alpha_t, T one_minus_beta1, T one_minus_beta2, '
                    'T eps, T eta, T weight_decay_rate',
                    'T param, T m, T v, T vhat',
                    '''m += one_minus_beta1 * (grad - m);
                       v += one_minus_beta2 * (grad * grad - v);
                       vhat = max(vhat, v);
                       param -= eta * (alpha_t * m / (sqrt(vhat) + eps) +
                                       weight_decay_rate * param);''',
                    'adam')
            AdamRule._amsgrad_kernel(
                grad, self.alpha_t, 1 - hp.beta1,
                1 - hp.beta2, hp.eps,
                hp.eta, hp.weight_decay_rate,
                param.data, self.state['m'], self.state['v'],
                self.state['vhat'])
        else:
            if AdamRule._kernel is None:
                AdamRule._kernel = cuda.elementwise(
                    'T grad, T alpha_t, T one_minus_beta1, T one_minus_beta2, '
                    'T eps, T eta, T weight_decay_rate',
                    'T param, T m, T v',
                    '''m += one_minus_beta1 * (grad - m);
                       v += one_minus_beta2 * (grad * grad - v);
                       param -= eta * (alpha_t * m / (sqrt(v) + eps) +
                                       weight_decay_rate * param);''',
                    'adam')
            AdamRule._kernel(grad, self.alpha_t, 1 - hp.beta1,
                             1 - hp.beta2, hp.eps,
                             hp.eta, hp.weight_decay_rate,
                             param.data, self.state['m'], self.state['v'])

    @property
    def alpha_t(self):
        return _learning_rate(self.hyperparam, self.t)

    @property
    def lr(self):
        warnings.warn(
            'AdamRule.lr has been renamed to AdamRule.alpha_t. '
            'Use of AdamRule.lr is deprecated in Chainer v6.',
            DeprecationWarning)
        return self.alpha_t

    @property
    def bounds(self):
        if self.t == 0:
            raise RuntimeError(
                'Can\'t determine the bounds of AdaBound optimizer '
                'because the update steps have not been started.')
        hp = self.hyperparam
        final_lr = hp.final_lr_rate * hp.alpha
        lower = final_lr * (1.0 - 1.0 / (hp.gamma * self.t + 1))
        upper = final_lr * (1.0 + 1.0 / (hp.gamma * self.t))
        #from lpu.common import logging
        #logger = logging.getColorLogger(__name__)
        #logger.debug_print(lower)
        #logger.debug_print(upper)
        return lower, upper


class Adam(optimizer.GradientMethod):

    """Adam optimizer.
    See: `Adam: A Method for Stochastic Optimization \
          <https://arxiv.org/abs/1412.6980v8>`_
    Modified for proper weight decay (also called AdamW).
    AdamW introduces the additional parameters ``eta``
    and ``weight_decay_rate``, which can be used to properly scale the
    learning rate, and decouple the weight decay rate from ``alpha``,
    as shown in the below paper.
    Note that with the default values ``eta = 1`` and
    ``weight_decay_rate = 0``, this implementation is identical to
    the standard Adam method.
    See: `Fixing Weight Decay Regularization in Adam \
          <https://openreview.net/forum?id=rk6qdGgCZ>`_
    A flag ``amsgrad`` to use the AMSGrad variant of Adam from
    the paper: `On the Convergence of Adam and Beyond \
               <https://openreview.net/forum?id=ryQu7f-RZ>`_
    A flag ``adabound`` to use the AdaBound variant of Adam from
    the paper: `Adaptive Gradient Methods with Dynamic Bound of Learning Rate \
               <https://openreview.net/forum?id=Bkg3g2R9FX>`_
    Args:
        alpha (float): Coefficient of learning rate.
        beta1 (float): Exponential decay rate of the first order moment.
        beta2 (float): Exponential decay rate of the second order moment.
        eps (float): Small value for the numerical stability.
        eta (float): Schedule multiplier, can be used for warm restarts.
        weight_decay_rate (float): Weight decay rate.
        amsgrad (bool): Whether to use AMSGrad variant of Adam.
        adabound (bool): Whether to use the AdaBound variant of Adam.
        final_lr_rate (float): final (SGD) learning rate per base-alpha in adabound.
        gamma (float): convergence speed of the bound functions in adabound.
    """

    def __init__(self,
                 alpha=_default_hyperparam.alpha,
                 beta1=_default_hyperparam.beta1,
                 beta2=_default_hyperparam.beta2,
                 eps=_default_hyperparam.eps,
                 eta=_default_hyperparam.eta,
                 weight_decay_rate=_default_hyperparam.weight_decay_rate,
                 amsgrad=_default_hyperparam.amsgrad,
                 adabound=_default_hyperparam.adabound,
                 final_lr_rate=_default_hyperparam.final_lr_rate,
                 gamma=_default_hyperparam.gamma):
        super(Adam, self).__init__()
        self.hyperparam.alpha = alpha
        self.hyperparam.beta1 = beta1
        self.hyperparam.beta2 = beta2
        self.hyperparam.eps = eps
        self.hyperparam.eta = eta
        self.hyperparam.weight_decay_rate = weight_decay_rate
        self.hyperparam.amsgrad = amsgrad
        self.hyperparam.adabound = adabound
        self.hyperparam.final_lr_rate = final_lr_rate
        self.hyperparam.gamma = gamma

    alpha = optimizer.HyperparameterProxy('alpha')
    beta1 = optimizer.HyperparameterProxy('beta1')
    beta2 = optimizer.HyperparameterProxy('beta2')
    eps = optimizer.HyperparameterProxy('eps')
    eta = optimizer.HyperparameterProxy('eta')
    weight_decay_rate = optimizer.HyperparameterProxy('weight_decay_rate')
    amsgrad = optimizer.HyperparameterProxy('amsgrad')
    adabound = optimizer.HyperparameterProxy('adabound')
    final_lr_rate = optimizer.HyperparameterProxy('final_lr_rate')
    gamma = optimizer.HyperparameterProxy('gamma')

    def create_update_rule(self):
        return AdamRule(self.hyperparam)

    @property
    def alpha_t(self):
        return _learning_rate(self.hyperparam, self.t)

    @property
    def lr(self):
        #from lpu.common import logging
        #logger = logging.getColorLogger(__name__)
        #logger.debug_print(self.bounds)
        warnings.warn(
            'Adam.lr has been renamed to AdamRule.alpha_t. '
            'Use of Adam.lr is deprecated in Chainer v6.',
            DeprecationWarning)
        return self.alpha_t

