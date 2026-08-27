import urllib.parse

import backup_status
import platform_admin
import server

_installed=False


def _flat(handler):
    form=handler.form_data(max_bytes=32768) or {}
    return {k:(v[0] if isinstance(v,list) and v else v) for k,v in form.items()}


def install():
    global _installed
    if _installed:return
    _installed=True
    old_post=server.StockroomHandler.do_POST

    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        if path not in ('/api/platform-admin/restore-test','/api/platform-admin/restore'):
            return old_post(self)
        if not self.enforce_origin():return
        session=self.require_session(api=True)
        if not session:return
        if not platform_admin.is_platform_admin(session):
            self.send_json(403,{'error':'Alleen platformbeheer heeft toegang tot deze functie.'});return
        values=_flat(self)
        try:
            if path.endswith('/restore-test'):
                result=backup_status.run_restore_test(values.get('filename') or '')
            else:
                result=backup_status.run_restore(values.get('filename') or '',values.get('confirmation') or '')
            self.send_json(200,result)
        except PermissionError as e:self.send_json(403,{'error':str(e)})
        except ValueError as e:self.send_json(400,{'error':str(e)})
        except TimeoutError:self.send_json(504,{'error':'Restore duurde te lang en is afgebroken.'})
        except Exception as e:self.send_json(500,{'error':str(e)[:1000] or 'Restore mislukt.'})

    server.StockroomHandler.do_POST=do_POST
