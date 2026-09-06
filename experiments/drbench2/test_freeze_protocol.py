import copy
import unittest
from unittest.mock import patch
import freeze_protocol as f

class FrozenProtocolTests(unittest.TestCase):
    def setUp(self):
        self.config=f.read(f.DEST/'multi.config.json')

    def test_multi_keeps_entire_default_configuration(self):
        import yaml
        original=yaml.safe_load((f.SNAPSHOT/'configs/default.yaml').read_text(encoding='utf-8'))
        self.assertEqual(self.config,original)
        self.assertEqual(f.build_arm(original,'multi'),original)

    def test_single_changes_only_recursive_limits(self):
        differences=f.differences(self.config,f.build_arm(self.config,'single'))
        expected={'max_fork_depth','max_children_per_agent','max_children','max_total_agents','max_total_threads','max_concurrent_agents'}
        self.assertEqual(set(differences),{'research.limits.'+x for x in expected})

    def test_default_budget_and_paper_tool_retained(self):
        limits=self.config['research']['limits']
        self.assertEqual(limits['max_total_tokens'],700000)
        self.assertEqual(limits['max_elapsed_seconds'],1200)
        self.assertEqual(limits['root_finalization_grace_seconds'],300)
        self.assertIn('arxiv_reader',self.config['tools']['enabled'])

    def test_environment_drift_is_rejected(self):
        changed=copy.deepcopy(f.read(f.DEST/'manifest.json'))
        changed['observed_environment']['research_model']['temperature']=0.7
        with patch.object(f,'collect',return_value=(self.config,changed)):
            with self.assertRaises(ValueError):
                f.verify()

    def test_network_is_explicitly_denied(self):
        with self.assertRaises(RuntimeError):
            f.deny_network(('example.com',443))

if __name__=='__main__':
    unittest.main()
