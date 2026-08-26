import unittest
from unittest import mock

import documents_v2


ORDER={
    'id':'o1','order_type':'sales','order_number':'SO-2026-000001','reference':'REF-1','relation_name':'Klant BV',
    'order_date':'2026-08-26','status':'completed','stockroom_name':'Demo Stockroom','company_name':'Demo BV','address':'Straat 1',
    'postal_code':'7500AA','city':'Enschede','country':'NL','vat_number':'NL123','chamber_number':'12345678','invoice_email':'factuur@example.nl',
    'iban':'NL00BANK0000000000','bic':'BANKNL2A','payment_term_days':14,'default_vat_percent':21.0,'invoice_footer':'Bedankt','accent_hex':'#111827','logo_data':''
}
LINES=[{'item_name':'Artikel A','sku':'SKU-A','quantity':2.0,'unit_price':12.5,'fulfilled_quantity':2.0}]
REL={'name':'Klant BV','contact_name':'Jan','address':'Klantstraat 2','email':'klant@example.nl'}
INV={'invoice_number':'INV-2026-000001','invoice_date':'2026-08-26','due_date':'2026-09-09','vat_percent':21.0}


class DocumentV2Tests(unittest.TestCase):
    def test_invoice_is_pdf_and_named_by_invoice(self):
        with mock.patch('documents_v2._load',return_value=(dict(ORDER),LINES,REL)), mock.patch('documents_v3.ensure_invoice',return_value=dict(INV)):
            data,name=documents_v2.invoice_pdf('room','o1')
        self.assertTrue(data.startswith(b'%PDF'))
        self.assertEqual(name,'factuur-INV-2026-000001.pdf')

    def test_invoice_rejects_purchase_order(self):
        purchase=dict(ORDER);purchase['order_type']='purchase'
        with mock.patch('documents_v2._load',return_value=(purchase,LINES,REL)):
            with self.assertRaises(ValueError):documents_v2.invoice_pdf('room','o1')

    def test_packing_slip_and_return_are_pdf(self):
        with mock.patch('documents_v2._load',return_value=(dict(ORDER),LINES,REL)):
            packing,_=documents_v2.packing_slip_pdf('room','o1')
            ret,_=documents_v2.return_pdf('room','o1')
        self.assertTrue(packing.startswith(b'%PDF'))
        self.assertTrue(ret.startswith(b'%PDF'))


if __name__=='__main__':unittest.main()
