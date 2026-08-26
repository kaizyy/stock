import urllib.parse

import server
import documents_v2

_installed=False


def install():
    global _installed
    if _installed:return
    _installed=True
    old_get=server.StockroomHandler.do_GET
    def do_GET(self):
        p=urllib.parse.urlparse(self.path);path=p.path
        if path in ('/api/documents/invoice.pdf','/api/documents/packing-slip.pdf','/api/documents/return.pdf'):
            s=self.require_session(api=True)
            if not s:return
            oid=urllib.parse.parse_qs(p.query).get('id',[''])[0]
            try:
                if path.endswith('invoice.pdf'):data,name=documents_v2.invoice_pdf(s['stockroom_id'],oid)
                elif path.endswith('packing-slip.pdf'):data,name=documents_v2.packing_slip_pdf(s['stockroom_id'],oid)
                else:data,name=documents_v2.return_pdf(s['stockroom_id'],oid)
                self.send_response(200);self.send_header('Content-Type','application/pdf');self.send_header('Content-Disposition',f'inline; filename="{name}"');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            except PermissionError as e:self.send_json(404,{'error':str(e)})
            except ValueError as e:self.send_json(400,{'error':str(e)})
            return
        return old_get(self)
    server.StockroomHandler.do_GET=do_GET
