# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Functional tests for pooling operations."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import os

import numpy as np

from tensorflow.python.eager import context
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import errors_impl
from tensorflow.python.framework import ops
from tensorflow.python.framework import test_util
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import gen_array_ops
from tensorflow.python.ops import gen_nn_ops
from tensorflow.python.ops import gradient_checker
from tensorflow.python.ops import gradients_impl
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import variables
import tensorflow.python.ops.nn_grad  # pylint: disable=unused-import
from tensorflow.python.platform import test
from tensorflow.python.platform import tf_logging


def GetDeviceScope(self, use_gpu=False):
  if context.executing_eagerly():
    if use_gpu and test.is_gpu_available():
      return ops.device("GPU:0")
    return ops.device("CPU:0")
  else:
    return self.session(use_gpu=use_gpu)


def GetTestConfigs(include_nchw_vect_c=False, one_dimensional=False):
  """Get all the valid tests configs to run.

  Args:
    include_nchw_vect_c: Whether to include NCHW_VECT_C in the test configs.
    one_dimensional: If it's a 1D test

  Returns:
    all the valid test configs as tuples of data_format and use_gpu.
  """
  if one_dimensional:
    test_configs = [("NWC", False), ("NWC", True)]
    if test.is_gpu_available(cuda_only=True):
      test_configs += [("NCW", True)]
    return test_configs
  test_configs = [("NHWC", False), ("NHWC", True)]
  if not test.is_gpu_available(cuda_only=True):
    tf_logging.info("NCHW and NCHW_VECT_C tests skipped because not run with "
                    "--config=cuda or no GPUs available.")
    return test_configs
  # "NCHW" format is currently supported exclusively on CUDA GPUs.
  test_configs += [("NCHW", True)]
  if include_nchw_vect_c:
    if test.is_gpu_available(
        cuda_only=True, min_cuda_compute_capability=(6, 1)):
      test_configs += [("NCHW_VECT_C", True)]
    else:
      tf_logging.info("NCHW_VECT_C test skipped because no GPUs with "
                      "compute capability >= 6.1 are available.")

  return test_configs


def GetShrunkInceptionMaxPoolShapes(shrink=30):
  """Iterator for some of the max pool ops in the Inception 2015 model.

  Args:
    shrink: Factor to shrink depth relative to Inception.

  Yields:
    Tuple (name, input_size, filter_size, out_size, strides, padding)
  """
  names = ["maxpool2", "maxpool3", "maxpool4", "maxpool5"]
  input_sizes = [[32, 71, 71, 192], [32, 35, 35, 288], [32, 17, 17, 1248],
                 [32, 8, 8, 2048]]
  filter_sizes = [[1, 3, 3, 1], [1, 3, 3, 1], [1, 3, 3, 1], [1, 3, 3, 1]]
  output_sizes = [[32, 35, 35, 192], [32, 17, 17, 288], [32, 8, 8, 1248],
                  [32, 8, 8, 2048]]
  strides = [[1, 2, 2, 1], [1, 2, 2, 1], [1, 2, 2, 1], [1, 1, 1, 1]]
  # Shrink each depth value
  for i in input_sizes:
    i[3] //= shrink
  for o in output_sizes:
    o[3] //= shrink
  paddings = ["VALID", "VALID", "VALID", "SAME"]
  for n, i, f, o, s, p in zip(names, input_sizes, filter_sizes, output_sizes,
                              strides, paddings):
    yield n, i, f, o, s, p


class PoolingTest(test.TestCase):

  def _isMaxPool(self, func):
    return func in (nn_ops.max_pool)

  def _VerifyOneType(self, pool_func, input_sizes, ksize, strides, padding,
                     data_format, data_type, expected, use_gpu, v2,
                     use_negative_input=False):
    """Verifies the output values of the pooling function.

    Args:
      pool_func: Function to be called, co.MaxPool, co.AvgPool,
        or the Lua version.
      input_sizes: Input tensor dimensions.
      ksize: The kernel size dimensions
      strides: The stride dimensions
      padding: Padding type.
      data_format: The data format we use to run the pooling operation.
      data_type: The data type to use to run the pooling operation.
      expected: An array containing the expected operation outputs.
      use_gpu: Whether we are running on GPU.
      v2: Whether to use v2 version.
      use_negative_input: If the input values should be negative.
    """
    total_size = 1
    for s in input_sizes:
      total_size *= s
    if v2 and data_format != "NHWC":
      tf_logging.info("v2 not supported for %s", data_format)
      return
    if data_format == "NCHW_VECT_C":
      if data_type != dtypes.float32:
        tf_logging.info("quantization to qint8 not implemented for %r",
                        data_type)
        return
      if input_sizes[-1] % 4 != 0:
        tf_logging.info("Skipping test for depth %d", input_sizes[-1])
        return
    tf_logging.info("Running %s test. %r %r %d %r %r %r %s", data_format, v2,
                    input_sizes, total_size, pool_func, ksize, strides,
                    data_type)
    # Initializes the input tensor with array containing incrementing
    # numbers from 1, wrapping round to -127 after 127 to support int8.
    y = -1 if use_negative_input else 1
    x = [(((f + 128) % 255) - 127)*y for f in range(total_size)]
    with self.cached_session(use_gpu=use_gpu):
      t = constant_op.constant(x, shape=input_sizes, dtype=data_type)
      if data_format in ("NCHW", "NCHW_VECT_C", "NCW"):
        if data_format == "NCHW_VECT_C":
          t = test_util.NHWCToNCHW_VECT_C(t)
          t, _, _ = gen_array_ops.quantize_v2(t, -128.0, 127.0, dtypes.qint8)
        else:
          t = test_util.NHWCToNCHW(t)
        ksize = test_util.NHWCToNCHW(ksize)
        strides = test_util.NHWCToNCHW(strides)
        if isinstance(padding, list):
          padding = test_util.NHWCToNCHW(padding)
      ksize_placeholder = array_ops.placeholder(dtypes.int32, shape=[4])
      strides_placeholder = array_ops.placeholder(dtypes.int32, shape=[4])
      if v2:
        t = pool_func(
            t,
            ksize=ksize_placeholder,
            strides=strides_placeholder,
            padding=padding,
            data_format=data_format)
      else:
        t = pool_func(
            t,
            ksize=ksize,
            strides=strides,
            padding=padding,
            data_format=data_format)
      if data_format == "NCHW_VECT_C":
        t = gen_array_ops.dequantize(t, -128, 127)
        t = test_util.NCHW_VECT_CToNHWC(t)
      elif data_format == "NCHW":
        t = test_util.NCHWToNHWC(t)
      if v2:
        actual = t.eval(feed_dict={
            ksize_placeholder: ksize,
            strides_placeholder: strides
        })
      else:
        actual = self.evaluate(t)
        self.assertShapeEqual(actual, t)
      self.assertAllCloseAccordingToType(expected, actual.flatten())

  def _VerifyOneTest(self, pool_func, input_sizes, ksize, strides, padding,
                     data_format, expected, use_gpu, v2,
                     use_negative_input=False):
    """Verifies the output values of the pooling function.

    Args:
      pool_func: Function to be called, co.MaxPool, co.AvgPool,
        or the Lua version.
      input_sizes: Input tensor dimensions.
      ksize: The kernel size dimensions
      strides: The stride dimensions
      padding: Padding type.
      data_format: The data format we use to run the pooling operation.
      expected: An array containing the expected operation outputs.
      use_gpu: Whether we are running on GPU.
      v2: Whether to use v2 version.
      use_negative_input: If the input values should be negative."
    """
    if data_format == "NCHW_VECT_C":
      avg_pool_func = nn_ops.avg_pool
      tf_logging.info("pool_func=%s", pool_func)
      if pool_func == avg_pool_func:
        tf_logging.info("NCHW_VECT_C not yet implemented for avg_pool")
        return
      if (self._isMaxPool(pool_func) and isinstance(padding, list)):
        tf_logging.info("NCHW_VECT_C not yet implemented for max pool" +
                        " with explicit padding")
        return

    self._VerifyOneType(pool_func, input_sizes, ksize, strides, padding,
                        data_format, dtypes.float32, expected, use_gpu, v2,
                        use_negative_input)

    if not use_gpu or test_util.GpuSupportsHalfMatMulAndConv():
      self._VerifyOneType(pool_func, input_sizes, ksize, strides, padding,
                          data_format, dtypes.float16, expected, use_gpu, v2,
                          use_negative_input)

  def _VerifyValues(self,
                    pool_func,
                    input_sizes,
                    ksize,
                    strides,
                    padding,
                    expected,
                    use_gpu,
                    v2=False,
                    one_dim=False,
                    use_negative_input=False):
    """Verifies the output values of the pooling function.

    Args:
      pool_func: Function to be called, co.MaxPool, co.AvgPool,
        or the Lua version.
      input_sizes: Input tensor dimensions.
      ksize: The kernel size dimensions
      strides: The stride dimensions
      padding: Padding type.
      expected: An array containing the expected operation outputs.
      use_gpu: Whether we are running on GPU.
      v2: Whether to use v2 version.
      one_dim: If one dimensional pools should be done instead of two
        dimensional pools.
      use_negative_input: If the input values should be negative.
    """
    for (data_format, use_gpu_2) in GetTestConfigs(True, one_dim):
      if use_gpu_2 == use_gpu:
        self._VerifyOneTest(pool_func, input_sizes, ksize, strides, padding,
                            data_format, expected, use_gpu, v2,
                            use_negative_input)

  def _testAvgPoolValidPadding(self, use_gpu):
    expected_output = [7.0, 8.0, 9.0]
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[1, 3, 3, 3],
        ksize=[1, 2, 2, 1],
        strides=[1, 2, 2, 1],
        padding="VALID",
        expected=expected_output,
        use_gpu=use_gpu)

  def _testAvgPoolEmpty(self, use_gpu):
    expected_output = [7.0, 8.0, 9.0]
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[1, 3, 3, 0],
        ksize=[1, 2, 2, 1],
        strides=[1, 2, 2, 1],
        padding="VALID",
        expected=expected_output,
        use_gpu=use_gpu)

  def _testAvgPoolSamePadding(self, use_gpu):
    expected_output = [8.5, 9.5, 10.5, 14.5, 15.5, 16.5]
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[1, 2, 4, 3],
        ksize=[1, 2, 2, 1],
        strides=[1, 2, 2, 1],
        padding="SAME",
        expected=expected_output,
        use_gpu=use_gpu)

  def _testAvgPoolSamePaddingNonSquareWindow(self, use_gpu):
    # input is:
    # [1.0, 2.0
    #  3.0  4.0]
    #
    # Window of [x, x] should do:
    #  [avg(1.0, 2.0), avg(2.0, padded0),
    #   avg(3.0, 4.0), avg(4.0, padded0)]
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[1, 2, 2, 1],
        ksize=[1, 1, 2, 1],
        strides=[1, 1, 1, 1],
        padding="SAME",
        expected=[1.5, 2.0, 3.5, 4.0],
        use_gpu=use_gpu)

    # Window of [x,
    #            x] should do:
    #  [avg(1.0, 3.0), avg(2.0, 4.0)
    #   avg(3.0, padded0), avg(4.0, padded0)]
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[1, 2, 2, 1],
        ksize=[1, 2, 1, 1],
        strides=[1, 1, 1, 1],
        padding="SAME",
        expected=[2.0, 3.0, 3.0, 4.0],
        use_gpu=use_gpu)

  def _testAvgPoolSamePaddingNonSquareWindowMultiBatch(self, use_gpu):
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[2, 2, 2, 2],
        ksize=[1, 1, 2, 1],
        strides=[1, 1, 1, 1],
        padding="SAME",
        expected=[
            2.0, 3.0, 3.0, 4.0, 6.0, 7.0, 7.0, 8.0, 10.0, 11.0, 11.0, 12.0,
            14.0, 15.0, 15.0, 16.0
        ],
        use_gpu=use_gpu)
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[2, 2, 2, 2],
        ksize=[1, 2, 1, 1],
        strides=[1, 1, 1, 1],
        padding="SAME",
        expected=[
            3.0, 4.0, 5.0, 6.0, 5.0, 6.0, 7.0, 8.0, 11.0, 12.0, 13.0, 14.0,
            13.0, 14.0, 15.0, 16.0
        ],
        use_gpu=use_gpu)

  def _testAvgPoolValidPaddingUnevenStride(self, use_gpu):
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[1, 3, 3, 3],
        ksize=[1, 2, 2, 1],
        strides=[1, 1, 2, 1],
        padding="VALID",
        expected=[7.0, 8.0, 9.0, 16.0, 17.0, 18.0],
        use_gpu=use_gpu)
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[1, 3, 3, 3],
        ksize=[1, 2, 2, 1],
        strides=[1, 2, 1, 1],
        padding="VALID",
        expected=[7.0, 8.0, 9.0, 10.0, 11.0, 12.0],
        use_gpu=use_gpu)

  def _testAvgPoolSamePadding4(self, use_gpu):
    expected_output = [
        11.0, 12.0, 13.0, 14.0, 19.0, 20.0, 21.0, 22.0, 43.0, 44.0, 45.0, 46.0,
        51.0, 52.0, 53.0, 54.0
    ]
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[1, 4, 4, 4],
        ksize=[1, 2, 2, 1],
        strides=[1, 2, 2, 1],
        padding="SAME",
        expected=expected_output,
        use_gpu=use_gpu)

  def _testAvgPoolSamePaddingPacket4(self, use_gpu):
    expected_output = [
        21.0, 22.0, 23.0, 24.0, 27.0, 28.0, 29.0, 30.0, 45.0, 46.0, 47.0, 48.0,
        51.0, 52.0, 53.0, 54.0
    ]
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[1, 4, 4, 4],
        ksize=[1, 3, 3, 1],
        strides=[1, 2, 2, 1],
        padding="SAME",
        expected=expected_output,
        use_gpu=use_gpu)

  def _testAvgPoolSamePaddingPacket8(self, use_gpu):
    expected_output = [
        -12.0, -11.0, -10.0, -9.0, -8.0, -7.0, -6.0, -5.0, 4.0, 5.0, 6.0, 7.0,
        8.0, 9.0, 10.0, 11.0, 20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0,
        32.0, 33.0, 34.0, 35.0, 36.0, 37.0, 38.0, -3.5, -54.0, -53.0, -52.0,
        -51.0, -50.0, -49.0, -48.0, -47.0, -38.0, -37.0, -36.0, -35.0, -34.0,
        -33.0, -32.0, -31.0, -22.0, -21.0, -20.0, -19.0, -18.0, -17.0, -16.0,
        -15.0, -10.0, -9.0, -8.0, -7.0, -6.0, -5.0, -4.0, -3.0, -11.0, -10.0,
        -9.0, -8.0, -7.0, -6.0, -5.0, -4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0,
        12.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 33.0, 34.0, 35.0,
        36.0, 37.0, 38.0, -3.5, -2.5, -85.0, -84.0, -83.0, -82.0, -81.0, -80.0,
        -79.0, -78.0, -69.0, -68.0, -67.0, -66.0, -65.0, -64.0, -63.0, -62.0,
        -53.0, -52.0, -51.0, -50.0, -49.0, -48.0, -47.0, -46.0, -41.0, -40.0,
        -39.0, -38.0, -37.0, -36.0, -35.0, -34.0
    ]

    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[1, 8, 8, 8],
        ksize=[1, 3, 3, 1],
        strides=[1, 2, 2, 1],
        padding="SAME",
        expected=expected_output,
        use_gpu=use_gpu)

  def _testAvgPoolEmptyInput(self, use_gpu):
    self._VerifyValues(
        nn_ops.avg_pool,
        input_sizes=[0, 8, 8, 8],
        ksize=[1, 3, 3, 1],
        strides=[1, 2, 2, 1],
        padding="SAME",
        expected=[],
        use_gpu=use_gpu)

  @test_util.run_deprecated_v1
  def testAvgPooling(self):
    for use_gpu in True, False:
      self._testAvgPoolValidPadding(use_gpu)
      self._testAvgPoolSamePadding(use_gpu)
      self._testAvgPoolSamePaddingNonSquareWindow(use_gpu)
      self._testAvgPoolSamePaddingNonSquareWindowMultiBatch(use_gpu)
      self._testAvgPoolValidPaddingUnevenStride(use_gpu)
      self._testAvgPoolSamePadding4(use_gpu)
      self._testAvgPoolSamePaddingPacket4(use_gpu)
      self._testAvgPoolSamePaddingPacket8(use_gpu)
      self._testAvgPoolEmptyInput(use_gpu)

  def _testMaxPoolValidPadding(self, use_gpu):
    expected_output = [13.0, 14.0, 15.0]
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 3, 3, 3],
        ksize=[1, 2, 2, 1],
        strides=[1, 2, 2, 1],
        padding="VALID",
        expected=expected_output,
        use_gpu=use_gpu)

  def _testMaxPoolSamePadding(self, use_gpu):
    expected_output = [13.0, 14.0, 15.0, 16.0, 17.0, 18.0]
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 2, 3, 3],
        ksize=[1, 2, 2, 1],
        strides=[1, 2, 2, 1],
        padding="SAME",
        expected=expected_output,
        use_gpu=use_gpu)

  def _testMaxPoolZeroExplicitPadding(self, use_gpu):
    expected_output = [9.0]
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 3, 3, 1],
        ksize=[1, 3, 3, 1],
        strides=[1, 2, 2, 1],
        padding=[[0, 0], [0, 0], [0, 0], [0, 0]],
        expected=expected_output,
        use_gpu=use_gpu)

  def _testMaxPoolNegativeInputExpPadding(self, use_gpu):
    expected_output = [-1, -1, -1, -1]
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 3, 3, 1],
        ksize=[1, 3, 3, 1],
        strides=[1, 2, 2, 1],
        padding=[[0, 0], [2, 1], [2, 1], [0, 0]],
        expected=expected_output,
        use_gpu=use_gpu,
        use_negative_input=True)

  def _testMaxPoolExplicitPadding(self, use_gpu):
    expected_output = [9.0, 9.0]
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 3, 3, 1],
        ksize=[1, 3, 3, 1],
        strides=[1, 2, 2, 1],
        padding=[[0, 0], [0, 2], [0, 1], [0, 0]],
        expected=expected_output,
        use_gpu=use_gpu)

  def _testMaxPoolExplicitPaddingAdvanced(self, use_gpu):
    expected_output = [7, 9, 11, 12, 19, 21, 23, 24, 31, 33, 35, 36, 31, 33,
                       35, 36]
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 6, 6, 1],
        ksize=[1, 3, 3, 1],
        strides=[1, 2, 2, 1],
        padding=[[0, 0], [1, 2], [2, 1], [0, 0]],
        expected=expected_output,
        use_gpu=use_gpu)

  def _testMaxPoolNegativeInputExpPaddingAdv(self, use_gpu):
    expected_output = [-1, -1, -3, -5, -7, -7, -9, -11, -19, -19, -21, -23, -31,
                       -31, -33, -35]

    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 6, 6, 1],
        ksize=[1, 3, 3, 1],
        strides=[1, 2, 2, 1],
        padding=[[0, 0], [1, 2], [2, 1], [0, 0]],
        expected=expected_output,
        use_gpu=use_gpu,
        use_negative_input=True)

  def _testMaxPoolExplicitPadding1D(self, use_gpu):
    expected_output = [2.0, 3.0]
    self._VerifyValues(
        nn_ops.max_pool1d,
        input_sizes=[1, 3, 1],
        ksize=[1, 2, 1],
        strides=[1, 2, 1],
        padding=[[0, 0], [0, 1], [0, 0]],
        expected=expected_output,
        use_gpu=use_gpu,
        one_dim=True)

  def _testMaxPoolExplicitPadding1dV2(self, use_gpu):
    expected_output = [2.0, 3.0]
    self._VerifyValues(
        nn_ops.max_pool_v2,
        input_sizes=[1, 3, 1],
        ksize=[1, 2, 1],
        strides=[1, 2, 1],
        padding=[[0, 0], [0, 1], [0, 0]],
        expected=expected_output,
        use_gpu=use_gpu,
        one_dim=True)

  def _testMaxPoolSamePaddingNonSquareWindow(self, use_gpu):
    # input is:
    # [1.0, 2.0
    #  3.0  4.0]
    #
    # Window of [x, x] should do:
    #
    #  [max(1.0, 2.0), max(2.0, padded0),
    #   max(3.0, 4.0), max(4.0, padded0)]
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 2, 2, 1],
        ksize=[1, 1, 2, 1],
        strides=[1, 1, 1, 1],
        padding="SAME",
        expected=[2.0, 2.0, 4.0, 4.0],
        use_gpu=use_gpu)

  def _testMaxPoolValidPaddingUnevenStride(self, use_gpu):
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 4, 4, 1],
        ksize=[1, 2, 2, 1],
        strides=[1, 1, 2, 1],
        padding="VALID",
        expected=[6.0, 8.0, 10.0, 12.0, 14.0, 16.0],
        use_gpu=use_gpu)
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 4, 4, 1],
        ksize=[1, 2, 2, 1],
        strides=[1, 2, 1, 1],
        padding="VALID",
        expected=[6.0, 7.0, 8.0, 14.0, 15.0, 16.0],
        use_gpu=use_gpu)

  def _testMaxPoolSamePaddingPacket4(self, use_gpu):
    expected_output = [
        21.0, 22.0, 23.0, 24.0, 29.0, 30.0, 31.0, 32.0, 53.0, 54.0, 55.0, 56.0,
        61.0, 62.0, 63.0, 64.0
    ]
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 4, 4, 4],
        ksize=[1, 2, 2, 1],
        strides=[1, 2, 2, 1],
        padding="SAME",
        expected=expected_output,
        use_gpu=use_gpu)

  def _testMaxPoolSamePaddingPacket8(self, use_gpu):
    expected_output = [
        81.0, 82.0, 83.0, 84.0, 85.0, 86.0, 87.0, 88.0, 97.0, 98.0, 99.0, 100.0,
        101.0, 102.0, 103.0, 104.0, 113.0, 114.0, 115.0, 116.0, 117.0, 118.0,
        119.0, 120.0, 121.0, 122.0, 123.0, 124.0, 125.0, 126.0, 127.0, 120.0,
        18.0, 19.0, 20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 34.0, 35.0, 36.0, 37.0,
        38.0, 39.0, 40.0, 41.0, 50.0, 51.0, 52.0, 53.0, 54.0, 55.0, 56.0, 57.0,
        58.0, 59.0, 60.0, 61.0, 62.0, 63.0, 64.0, 65.0, 82.0, 83.0, 84.0, 85.0,
        86.0, 87.0, 88.0, 89.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0,
        105.0, 114.0, 115.0, 116.0, 117.0, 118.0, 119.0, 120.0, 121.0, 122.0,
        123.0, 124.0, 125.0, 126.0, 127.0, 120.0, 121.0, -45.0, -44.0, -43.0,
        -42.0, -41.0, -40.0, -39.0, -38.0, -29.0, -28.0, -27.0, -26.0, -25.0,
        -24.0, -23.0, -22.0, -13.0, -12.0, -11.0, -10.0, -9.0, -8.0, -7.0, -6.0,
        -5.0, -4.0, -3.0, -2.0, -1.0, 0.0, 1.0, 2.0
    ]
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 8, 8, 8],
        ksize=[1, 3, 3, 1],
        strides=[1, 2, 2, 1],
        padding="SAME",
        expected=expected_output,
        use_gpu=use_gpu)

  def _testMaxPoolEmptyInput(self, use_gpu):
    self._VerifyValues(
        gen_nn_ops.max_pool_v2,
        input_sizes=[0, 8, 8, 8],
        ksize=[1, 3, 3, 1],
        strides=[1, 2, 2, 1],
        padding="SAME",
        expected=[],
        use_gpu=use_gpu)

  @test_util.run_deprecated_v1
  @test_util.xla_allow_fallback(
      "Allow VECT_* data formats on newer hardware versions which XLA does not"
      " handle."
  )
  def testMaxPooling(self):
    for use_gpu in True, False:
      self._testMaxPoolValidPadding(use_gpu)
      self._testMaxPoolSamePadding(use_gpu)
      self._testMaxPoolSamePaddingNonSquareWindow(use_gpu)
      self._testMaxPoolValidPaddingUnevenStride(use_gpu)
      self._testMaxPoolSamePaddingPacket4(use_gpu)
      self._testMaxPoolSamePaddingPacket8(use_gpu)
      # self._testMaxPoolEmptyInput(use_gpu)
      self._testMaxPoolZeroExplicitPadding(use_gpu)
      self._testMaxPoolExplicitPadding(use_gpu)
      self._testMaxPoolExplicitPadding1D(use_gpu)
      # self._testMaxPoolExplicitPadding1dV2(use_gpu)
      self._testMaxPoolExplicitPaddingAdvanced(use_gpu)
      self._testMaxPoolNegativeInputExpPadding(use_gpu)
      self._testMaxPoolNegativeInputExpPaddingAdv(use_gpu)

  # Tests for DepthwiseMaxPooling on CPU only.
  @test_util.run_deprecated_v1
  def testDepthwiseMaxPool1x1DepthWindow1(self):
    # input is:
    # [1.0, ..., 10.0] along depth,
    #
    # We maxpool by depth in patches of 2.
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 1, 1, 10],
        ksize=[1, 1, 1, 2],
        strides=[1, 1, 1, 2],
        padding="SAME",
        expected=[2.0, 4.0, 6.0, 8.0, 10.0],
        use_gpu=False)

  @test_util.run_deprecated_v1
  def testDepthwiseMaxPool2x2DepthWindow3(self):
    # input is:
    #
    # a 2x2x6 cube, and we depthwise max across 3 to produce a 2x2x2
    # output.  Each node has contiguous values, so the depthwise max
    # should be multiples of 3.0.
    self._VerifyValues(
        nn_ops.max_pool,
        input_sizes=[1, 2, 2, 6],
        ksize=[1, 1, 1, 3],
        strides=[1, 1, 1, 3],
        padding="SAME",
        expected=[3.0, 6.0, 9.0, 12.0, 15.0, 18.0, 21.0, 24.0],
        use_gpu=False)

  @test_util.run_deprecated_v1
  def testKernelSmallerThanStrideValid(self):
    for use_gpu in [True, False]:
      self._VerifyValues(
          nn_ops.max_pool,
          input_sizes=[1, 7, 7, 1],
          ksize=[1, 2, 2, 1],
          strides=[1, 3, 3, 1],
          padding="VALID",
          expected=[9, 12, 30, 33],
          use_gpu=use_gpu)

      self._VerifyValues(
          nn_ops.avg_pool,
          input_sizes=[1, 7, 7, 1],
          ksize=[1, 2, 2, 1],
          strides=[1, 3, 3, 1],
          padding="VALID",
          expected=[5, 8, 26, 29],
          use_gpu=use_gpu)

  @test_util.run_deprecated_v1
  def testKernelSmallerThanStrideSame(self):
    for use_gpu in [True, False]:
      for pool_func in [nn_ops.max_pool, nn_ops.avg_pool]:
        self._VerifyValues(
            pool_func,
            input_sizes=[1, 3, 3, 1],
            ksize=[1, 1, 1, 1],
            strides=[1, 2, 2, 1],
            padding="SAME",
            expected=[1, 3, 7, 9],
            use_gpu=use_gpu)

        self._VerifyValues(
            pool_func,
            input_sizes=[1, 4, 4, 1],
            ksize=[1, 1, 1, 1],
            strides=[1, 2, 2, 1],
            padding="SAME",
            expected=[1, 3, 9, 11],
            use_gpu=use_gpu)

  def _testDepthwiseMaxPoolInvalidConfig(self,
                                         in_size,
                                         ksize,
                                         strides,
                                         error_msg,
                                         use_gpu=False):
    with self.cached_session(use_gpu=use_gpu):
      t = constant_op.constant(1.0, shape=in_size)
      with self.assertRaisesRegex(errors_impl.UnimplementedError, error_msg):
        t = nn_ops.max_pool(
            t, ksize=ksize, strides=strides, padding="SAME").eval()

  # The following are tests that verify that the CPU and GPU implementations
  # produce the same results.
  def _CompareMaxPoolingFwd(self, input_shape, ksize, strides, padding):
    # double datatype is currently not supported for pooling ops
    # on the ROCm platform
    for dtype in [np.float32, np.float16]:
      tensor_input = np.random.rand(*input_shape).astype(dtype)
      with self.cached_session(use_gpu=True):
        t = constant_op.constant(tensor_input, shape=input_shape)
        out_op, _ = nn_ops.max_pool_with_argmax(t, ksize, strides, padding)
        gpu_val = self.evaluate(out_op)
      with self.cached_session(use_gpu=False):
        t = constant_op.constant(tensor_input, shape=input_shape)
        out_op = nn_ops.max_pool(t, ksize, strides, padding)
        cpu_val = self.evaluate(out_op)
      self.assertAllCloseAccordingToType(cpu_val, gpu_val)

  def testMaxPoolingWithArgmax(self):
    tensor_input = [
        1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 1.0
    ]

    Config = collections.namedtuple(
        "Config", ["use_gpu", "include_batch_in_index", "argmax"])
    configs = [
        Config(False, False, [0, 1, 3, 5, 0, 2, 6, 8]),
        Config(False, True, [0, 1, 3, 5, 9, 11, 15, 17]),
        Config(True, False, [0, 1, 3, 5, 0, 2, 6, 8]),
        Config(True, True, [0, 1, 3, 5, 9, 11, 15, 17])
    ]

    for config in configs:
      with GetDeviceScope(self, use_gpu=config.use_gpu):
        t = constant_op.constant(tensor_input, shape=[2, 3, 3, 1])
        out_op, argmax_op = nn_ops.max_pool_with_argmax(
            t,
            ksize=[1, 2, 2, 1],
            strides=[1, 1, 1, 1],
            Targmax=dtypes.int64,
            padding="VALID",
            include_batch_in_index=config.include_batch_in_index)
        out, argmax = self.evaluate([out_op, argmax_op])
        self.assertShapeEqual(out, out_op)
        self.assertShapeEqual(argmax, argmax_op)
        self.assertAllClose(out.ravel(),
                            [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
        self.assertAllEqual(argmax.ravel(), config.argmax)

  @test_util.run_deprecated_v1
  def testShapeFunctionEdgeCases(self):
    # All shapes unknown.
    for pool_func in [nn_ops.max_pool, nn_ops.avg_pool]:
      p = pool_func(
          array_ops.placeholder(dtypes.float32),
          ksize=[1, 1, 1, 1],
          strides=[1, 1, 1, 1],
          padding="SAME")
      self.assertEqual([None, None, None, None], p.get_shape().as_list())
    p, am = nn_ops.max_pool_with_argmax(
        array_ops.placeholder(dtypes.float32),
        ksize=[1, 1, 1, 1],
        strides=[1, 1, 1, 1],
        padding="SAME")
    self.assertEqual([None, None, None, None], p.get_shape().as_list())
    self.assertEqual([None, None, None, None], am.get_shape().as_list())

    # Incorrect input shape.
    for pool_func in [
        nn_ops.max_pool, nn_ops.avg_pool, nn_ops.max_pool_with_argmax
    ]:
      with self.assertRaises(ValueError):
        pool_func(
            array_ops.placeholder(dtypes.float32, shape=[1, 3]),
            ksize=[1, 1, 1, 1],
            strides=[1, 1, 1, 1],
            padding="SAME")

  @test_util.run_deprecated_v1
  def _testEdgeCasesRaiseErrors(self):
    with self.assertRaisesRegexp(
        ValueError, "Data formats NCHW_VECT_C is not yet supported with "
                    "explicit padding"):
      nn_ops.max_pool(
          array_ops.placeholder(dtypes.float32, shape=[1, 3, 3, 1]),
          ksize=[1, 2, 2, 1],
          strides=[1, 2, 2, 1],
          padding=[[0, 0], [0, 1], [0, 1], [0, 0]],
          data_format="NCHW_VECT_C")

  @test_util.run_deprecated_v1
  def _testEdgeCasesExcessPadding(self):
    with self.session(use_gpu=test.is_gpu_available()) as sess:
      with self.assertRaisesRegexp(
          errors_impl.InvalidArgumentError,
          "Right padding 2 needs to be smaller than the window size 2"):
        input_sizes = [1, 3, 3, 1]
        x = [(((f + 128) % 255) - 127) for f in range(9)]
        t = constant_op.constant(x, shape=input_sizes, dtype=dtypes.float32)
        sess.run(gen_nn_ops.max_pool(
            t,
            ksize=[1, 2, 2, 1],
            strides=[1, 2, 2, 1],
            padding="EXPLICIT",
            explicit_paddings=[0, 0, 0, 1, 0, 2, 0, 0],
            data_format="NHWC"))

  @test_util.run_deprecated_v1
  def _testNegativePadding(self):
    with self.session(use_gpu=test.is_gpu_available()) as sess:
      with self.assertRaisesRegexp(
          ValueError, "All elements of explicit_paddings must be "
                      "nonnegative for"):
        input_sizes = [1, 3, 3, 1]
        x = [(((f + 128) % 255) - 127) for f in range(9)]
        t = constant_op.constant(x, shape=input_sizes, dtype=dtypes.float32)
        sess.run(gen_nn_ops.max_pool(
            t,
            ksize=[1, 2, 2, 1],
            strides=[1, 2, 2, 1],
            padding="EXPLICIT",
            explicit_paddings=[0, 0, -1, -1, -1, -1, 0, 0],
            data_format="NHWC"))

  @test_util.run_deprecated_v1
  def _testExplicitPaddingBatch(self):
    with self.session(use_gpu=test.is_gpu_available()) as sess:
      with self.assertRaisesRegexp(
          ValueError, "Nonzero explicit padding in the batch or depth "
                      "dimensions is not supported"):
        input_sizes = [1, 3, 3, 1]
        x = [(((f + 128) % 255) - 127) for f in range(9)]
        t = constant_op.constant(x, shape=input_sizes, dtype=dtypes.float32)
        sess.run(gen_nn_ops.max_pool(
            t,
            ksize=[1, 2, 2, 1],
            strides=[1, 2, 2, 1],
            padding="EXPLICIT",
            explicit_paddings=[1, 1, 1, 1, 1, 1, 0, 0],
            data_format="NHWC"))

def GetMaxPoolFwdTest(input_size, filter_size, strides, padding):

  def Test(self):
    # MaxPoolWithArgMax is implemented only on CUDA.
    if not test.is_gpu_available(cuda_only=True):
      return
    self._CompareMaxPoolingFwd(input_size, filter_size, strides, padding)

  return Test


if __name__ == "__main__":
  for (name_, input_size_, filter_size_, output_size_, stride_,
       padding_) in GetShrunkInceptionMaxPoolShapes():
    setattr(PoolingTest, "testMaxPoolFwd_" + name_,
            GetMaxPoolFwdTest(input_size_, filter_size_, stride_, padding_))
  test.main()
