import glob
import os
from datetime import datetime, timezone


def _iso(ts):
    return datetime.fromtimestamp(ts,timezone.utc).isoformat()


def backup_status():
    directory=os.environ.get('BACKUP_DIR','/data/backups')
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
    return {'backup_dir':directory,'retention_days':retention,'latest_backup':latest,'last_restore_test':restore,'healthy':not warnings,'warnings':warnings}
