import resource
import unittest
from unittest.mock import patch

from symbols import limit_worker_cpu


class WorkerBudgetTests(unittest.TestCase):
    def test_rounds_cpu_allowance_and_keeps_existing_limits(self):
        for existing, expected in [((resource.RLIM_INFINITY, resource.RLIM_INFINITY), (3, 4)),
                                   ((2, 8), (2, 4)), ((1, 1), (1, 1))]:
            with self.subTest(existing=existing), patch('resource.getrlimit', return_value=existing), patch('resource.setrlimit') as setter:
                limit_worker_cpu(2.2)
                setter.assert_called_once_with(resource.RLIMIT_CPU, expected)
