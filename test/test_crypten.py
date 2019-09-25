#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


import itertools
import logging
import math
import unittest
from test.multiprocess_test_case import MultiProcessTestCase, get_random_test_tensor

import crypten
import torch
from crypten.common.tensor_types import is_float_tensor
from crypten.mpc.primitives import ArithmeticSharedTensor, BinarySharedTensor


class TestCrypten(MultiProcessTestCase):
    """
        This class tests all member functions of crypten package
    """

    benchmarks_enabled = False

    def setUp(self):
        super().setUp()
        if self.rank >= 0:
            crypten.init()
            crypten.set_default_backend(crypten.mpc)

    def _check(self, encrypted_tensor, reference, msg, tolerance=None):
        if tolerance is None:
            tolerance = getattr(self, "default_tolerance", 0.05)
        tensor = encrypted_tensor.get_plain_text()

        # Check sizes match
        self.assertTrue(tensor.size() == reference.size(), msg)

        self.assertTrue(is_float_tensor(reference), "reference must be a float")
        diff = (tensor - reference).abs_()
        norm_diff = diff.div(tensor.abs() + reference.abs()).abs_()
        test_passed = norm_diff.le(tolerance) + diff.le(tolerance * 0.1)
        test_passed = test_passed.gt(0).all().item() == 1
        if not test_passed:
            logging.info(msg)
            logging.info("Result = %s;\nreference = %s" % (tensor, reference))
        self.assertTrue(test_passed, msg=msg)

    def test_cat_stack(self):
        """Tests concatenation and stacking of tensors"""
        tensor1 = get_random_test_tensor(size=(5, 5, 5, 5), is_float=True)
        tensor2 = get_random_test_tensor(size=(5, 5, 5, 5), is_float=True)

        for type1 in [lambda x: x, crypten.cryptensor]:
            encrypted1 = type1(tensor1)
            for type2 in [lambda x: x, crypten.cryptensor]:
                encrypted2 = type2(tensor2)

                for op in ["cat", "stack"]:
                    reference = getattr(torch, op)([tensor1, tensor2])
                    with self.benchmark(type=op) as bench:
                        for _ in bench.iters:
                            encrypted_out = getattr(crypten, op)(
                                [encrypted1, encrypted2]
                            )
                    self._check(encrypted_out, reference, "%s failed" % op)

                    for dim in range(4):
                        reference = getattr(torch, op)([tensor1, tensor2], dim=dim)
                        with self.benchmark(type=op, dim=dim) as bench:
                            for _ in bench.iters:
                                encrypted_out = getattr(crypten, op)(
                                    [encrypted1, encrypted2], dim=dim
                                )
                        self._check(encrypted_out, reference, "%s failed" % op)

    def test_rand(self):
        """Tests uniform random variable generation on [0, 1)"""
        for size in [(10,), (10, 10), (10, 10, 10)]:
            with self.benchmark(size=size) as bench:
                for _ in bench.iters:
                    randvec = crypten.rand(*size)
            self.assertTrue(randvec.size() == size, "Incorrect size")
            tensor = randvec.get_plain_text()
            self.assertTrue(
                (tensor >= 0).all() and (tensor < 1).all(), "Invalid values"
            )

        randvec = crypten.rand(int(1e6)).get_plain_text()
        mean = torch.mean(randvec)
        var = torch.var(randvec)
        self.assertTrue(torch.isclose(mean, torch.Tensor([0.5]), rtol=1e-3, atol=1e-3))
        self.assertTrue(
            torch.isclose(var, torch.Tensor([1.0 / 12]), rtol=1e-3, atol=1e-3)
        )

    def test_bernoulli(self):
        for size in [(10,), (10, 10), (10, 10, 10)]:
            probs = torch.rand(size)
            with self.benchmark(size=size) as bench:
                for _ in bench.iters:
                    randvec = crypten.bernoulli(probs)
            self.assertTrue(randvec.size() == size, "Incorrect size")
            tensor = randvec.get_plain_text()
            self.assertTrue(((tensor == 0) + (tensor == 1)).all(), "Invalid values")

        probs = torch.Tensor(int(1e6)).fill_(0.2)
        randvec = crypten.bernoulli(probs).get_plain_text()
        frac_zero = float((randvec == 0).sum()) / randvec.nelement()
        self.assertTrue(math.isclose(frac_zero, 0.8, rel_tol=1e-3, abs_tol=1e-3))

    def test_ptype(self):
        """Test that ptype attribute creates the correct type of encrypted tensor"""
        ptype_values = [crypten.arithmetic, crypten.binary]
        tensor_types = [ArithmeticSharedTensor, BinarySharedTensor]
        for i, curr_ptype in enumerate(ptype_values):
            tensor = get_random_test_tensor(is_float=False)
            encr_tensor = crypten.cryptensor(tensor, ptype=curr_ptype)
            assert isinstance(encr_tensor._tensor, tensor_types[i]), "ptype test failed"

    def test_save_load(self):
        """Test that crypten.save and crypten.load properly save and load tensors"""
        import tempfile

        filename = tempfile.NamedTemporaryFile(delete=True).name

        import os

        for dimensions in range(1, 5):
            # Create tensors with different sizes on each rank
            size = [crypten.communicator.get().get_rank() + 1] * dimensions
            size = tuple(size)
            tensor = torch.randn(size=size)

            for src in range(crypten.communicator.get().get_world_size()):
                crypten.save(tensor, filename, src=src)
                result = crypten.load(filename, src=src)

                reference_size = tuple([src + 1] * dimensions)
                self.assertEqual(result.size(), reference_size)

            # Test load with src=None
            syncd_filename = "/tmp/tmpsyncdfile"
            tensor = get_random_test_tensor()
            crypten.save(tensor, syncd_filename)
            result = crypten.load(syncd_filename)
            self.assertTrue(result.eq(tensor).all().item())

        # Only remove tempfile once
        if self.rank == 0 and os.path.exists(syncd_filename):
            os.remove(syncd_filename)

    def test_where(self):
        """Test that crypten.where properly conditions"""
        sizes = [(10,), (5, 10), (1, 5, 10)]
        y_types = [lambda x: x, crypten.cryptensor]

        for size, y_type in itertools.product(sizes, y_types):
            tensor1 = get_random_test_tensor(size=size, is_float=True)
            encrypted_tensor1 = crypten.cryptensor(tensor1)
            tensor2 = get_random_test_tensor(size=size, is_float=True)
            encrypted_tensor2 = y_type(tensor2)

            condition_tensor = (
                get_random_test_tensor(max_value=1, size=size, is_float=False) + 1
            )
            condition_encrypted = crypten.cryptensor(condition_tensor)
            condition_bool = condition_tensor.bool()

            reference_out = torch.where(condition_bool, tensor1, tensor2)

            encrypted_out = crypten.where(
                condition_bool, encrypted_tensor1, encrypted_tensor2
            )

            y_is_private = crypten.is_encrypted_tensor(tensor2)
            self._check(
                encrypted_out,
                reference_out,
                f"{'private' if y_is_private else 'public'} y "
                "where failed with public condition",
            )

            encrypted_out = encrypted_tensor1.where(
                condition_encrypted, encrypted_tensor2
            )
            self._check(
                encrypted_out,
                reference_out,
                f"{'private' if y_is_private else 'public'} y "
                "where failed with private condition",
            )


# This code only runs when executing the file outside the test harness (e.g.
# via the buck target test_mpc_benchmark)
if __name__ == "__main__":
    TestCrypten.benchmarks_enabled = True
    unittest.main()
