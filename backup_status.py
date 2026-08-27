import glob
import os
import subprocess
from datetime import datetime, timezone


def _iso(ts):
    return datetime.fromtimestamp(ts,timezone.utc).isoformat()


def _backup_dir():
    return os.environ.get('BACKUP_DIR','/data/backups')


def list_backups(limit=20):
    directory=_backup_dir()
    files=sorted(glob.glob(os.path.join(directory,'stockroom-*.dump')),key=lambda p:os.path.getmtime(p),reverse=True)
    out=[]
    for p in files[:max(1,min(int(limit),100))]:
        out.append({'filename':os.path.basename(p),'size_bytes':os.path.getsize(p),'created_at':_iso(os.path.getmtime(p)),'checksum_exists':os.path.exists(p+'.sha256')})
    return out


def _safe_backup_path(filename):
    name=os.path.basename(str(filename or '').strip())
    if not name or name!=filename or not name.startswith('stockroom-') or not name.endswith('.dump'):
        raise ValueError('Ongeldig backupbestand.')
    path=os.path.join(_backup_dir(),name)
    if not os.path.isfile(path):
        raise ValueError('Backupbestand bestaat niet.')
    return path


def run_restore_test(filename):
    path=_safe_backup_path(filename)
    target=(os.environ.get('RESTORE_DATABASE_URL') or '').strip()
    if not target:
        raise ValueError('RESTORE_DATABASE_URL is niet ingesteld voor restore-tests.')
    env=os.environ.copy();env['RESTORE_DATABASE_URL']=target
    p=subprocess.run(['/usr/local/bin/verify-restore.sh',path],env=env,capture_output=True,text=True,timeout=300)
    if p.returncode!=0:
        raise RuntimeError((p.stderr or p.stdout or 'Restore-test mislukt.').strip()[:1000])
    marker=os.path.join(_backup_dir(),'last-restore-test.ok')
    with open(marker,'w',encoding='utf-8') as f:
        f.write(f"{os.path.basename(path)} · restore-verification-ok")
    return {'tested':True,'filename':os.path.basename(path)}


def run_restore(filename, confirmation):
    if os.environ.get('ALLOW_PLATFORM_RESTORE','').strip()!='1':
        raise PermissionError('Database restore via Platformbeheer is niet ingeschakeld. Zet ALLOW_PLATFORM_RESTORE=1 in Coolify.')
    if str(confirmation or '').strip()!='HERSTEL':
        raise ValueError('Typ exact HERSTEL om de database restore te bevestigen.')
    target=(os.environ.get('PLATFORM_RESTORE_DATABASE_URL') or '').strip()
    if not target:
        raise ValueError('PLATFORM_RESTORE_DATABASE_URL is niet ingesteld.')
    path=_safe_backup_path(filename)
    env=os.environ.copy();env['RESTORE_DATABASE_URL']=target
    p=subprocess.run(['/usr/local/bin/restore-postgres.sh',path],env=env,capture_output=True,text=True,timeout=600)
    if p.returncode!=0:
        raise RuntimeError((p.stderr or p.stdout or 'Database restore mislukt.').strip()[:1000])
    return {'restored':True,'filename':os.path.basename(path)}


def backup_status():
    directory=_backup_dir()
    retention=int(os.environ.get('BACKUP_RETENTION_DAYS','14'))
    files=sorted(glob.glob(os.path.join(directory,'stockroom-*.dump')),key=lambda p:os.path.getmtime(p),reverse=True)
    latest=None
    if files:
        p=files[0];latest={'filename':os.path.basename(p),'size_bytes':os.path.getsize(p),'created_at':_iso(os.path.getmtime(p)),'checksum_exists':os.path.exists(p+'.sha256')}
    marker=os.path.join(directory,'last-restore-test.ok');restore=None
    if os.path.exists(marker):
        restore={'tested_at':_iso(os.path.getmtime(marker)),'details':open(marker,encoding='utf-8',errors='replace').read(500).strip()}
    warnings=[]
    if not latest:warnings.append('Nog geen PostgreSQL-backup gevonden.')
    elif (datetime.now(timezone.utc).timestamp()-os.path.getmtime(files[0]))>36*3600:warnings.append('Laatste backup is ouder dan 36 uur.')
    if latest and not latest['checksum_exists']:warnings.append('Checksum van laatste backup ontbreekt.')
    if not restore:warnings.append('Nog geen succesvolle restore-test geregistreerd.')
    elif (datetime.now(timezone.utc).timestamp()-os.path.getmtime(marker))>8*24*3600:warnings.append('Laatste restore-test is ouder dan 8 dagen.')
    return {'backup_dir':directory,'retention_days':retention,'latest_backup':latest,'backups':list_backups(),'last_restore_test':restore,'restore_enabled':os.environ.get('ALLOW_PLATFORM_RESTORE','').strip()=='1','restore_test_enabled':bool((os.environ.get('RESTORE_DATABASE_URL') or '').strip()),'healthy':not warnings,'warnings':warnings}
