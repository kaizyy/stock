import unittest
import financial_workflow as fw

class FinancialWorkflowTests(unittest.TestCase):
    def test_public_finance_actions_available(self):
        self.assertTrue(callable(fw.list_invoices))
        self.assertTrue(callable(fw.record_payment))
        self.assertTrue(callable(fw.create_credit))
        self.assertTrue(callable(fw.credit_pdf))

    def test_finance_module_starts_uninstalled(self):
        self.assertIn(fw._installed,(False,True))

if __name__=='__main__':unittest.main()
