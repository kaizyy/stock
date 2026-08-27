import urllib.parse
import server

_installed=False

def install():
    global _installed
    if _installed:return
    _installed=True
    old_post=server.StockroomHandler.do_POST
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        if path=='/api/stockrooms/rename':
            if not self.enforce_origin():return
            s=self.require_session(api=True)
            if not s:return
            if s.get('role') not in ('owner','admin'):
                self.send_json(403,{'error':'Alleen een owner of admin mag de stockroomnaam wijzigen.'});return
            form=self.form_data() or {}
            raw=form.get('name',[''])
            name=(raw[0] if isinstance(raw,list) and raw else raw or '').strip()
            if len(name)<2 or len(name)>120:
                self.send_json(400,{'error':'Stockroomnaam moet tussen 2 en 120 tekens lang zijn.'});return
            with server.db() as conn:
                duplicate=conn.execute("SELECT id FROM stockrooms WHERE lower(trim(name))=lower(trim(%s)) AND id<>%s LIMIT 1",(name,s['stockroom_id'])).fetchone()
                if duplicate:
                    self.send_json(409,{'error':'Deze stockroomnaam bestaat al. Kies een andere naam.'});return
                conn.execute("UPDATE stockrooms SET name=%s,updated_at=NOW() WHERE id=%s",(name,s['stockroom_id']))
                conn.commit()
            self.send_json(200,{'saved':True,'name':name});return
        return old_post(self)
    server.StockroomHandler.do_POST=do_POST
