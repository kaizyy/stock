import unittest
from datetime import date, timedelta

import financial_workflow as fw


class FinancialWorkflowTests(unittest.TestCase):
    def test_status_paid(self):
        status,outstanding=fw._status({'paid_amount':121,'due_date':date.today()},121,0)
        self.assertEqual(status,'paid');self.assertEqual(outstanding,0)

    def test_status_overdue(self):
        status,outstanding=fw._status({'paid_amount':0,'due_date':date.today()-timedelta(days=1),'sent_at':True},121,0)
        self.assertEqual(status,'overdue');self.assertEqual(outstanding,121)

    def test_status_partial_with_credit(self):
        status,outstanding=fw._status({'paid_amount':20,'due_date':date.today()+timedelta(days=10)},121,10)
        self.assertEqual(status,'partial');self.assertEqual(outstanding,91)

    def test_status_fully_credited(self):
        status,outstanding=fw._status({'paid_amount':0,'due_date':date.today()+timedelta(days=10)},121,121)
        self.assertEqual(status,'credited');self.assertEqual(outstanding,0)


if __name__=='__main__':unittest.main()
