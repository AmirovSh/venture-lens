"""Manual off-host demo backup and optional isolated PostgreSQL restore probe."""
import argparse
import hashlib
import json
import shlex
import tarfile
import uuid
from pathlib import Path
from ssh_client import connect, execute, ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-restore', action='store_true')
    args = parser.parse_args()
    with connect() as client:
        with client.open_sftp() as sftp:
            sftp.put(str(ROOT / 'platform/server/snapshot.py'), '/opt/venture-lens/snapshot.py')
        report = json.loads(execute(client, 'python3 /opt/venture-lens/snapshot.py', timeout=180).strip())
        remote = report['backup_directory']
        local = ROOT / '.platform/server-backups' / Path(remote).name
        local.mkdir(parents=True, exist_ok=True)
        with client.open_sftp() as sftp:
            for name in ['manifest.json', 'database.dump', 'sources.tar.gz']:
                sftp.get(remote + '/' + name, str(local / name))
        manifest = json.loads((local / 'manifest.json').read_text())
        assert manifest['completed'] and {x['file'] for x in manifest['files']} == {'database.dump', 'sources.tar.gz'}
        for entry in manifest['files']:
            with (local / entry['file']).open('rb') as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == entry['sha256'], 'Backup checksum mismatch'
        with tarfile.open(local / 'sources.tar.gz') as archive:
            sources = json.load(archive.extractfile('manifest.json'))
            for entry in sources['objects']:
                with archive.extractfile(entry['key']) as stream:
                    assert hashlib.file_digest(stream, 'sha256').hexdigest() == entry['sha256'], 'Source checksum mismatch'
        proof = {'backup_directory': str(local), 'remote_directory': remote, 'files_verified': len(manifest['files']),
                 'source_objects_verified': len(sources['objects'])}
        if args.verify_restore:
            probe = 'venturelens_restore_probe_' + uuid.uuid4().hex[:12]
            command = 'k3s kubectl -n venture-lens-demo exec postgres-0 -- sh -c '
            execute(client, command + shlex.quote('createdb -U "$POSTGRES_USER" ' + probe))
            try:
                execute(client, 'set -euo pipefail\ncat ' + shlex.quote(remote + '/database.dump') + ' | '
                        + command.replace(' exec ', ' exec -i ')
                        + shlex.quote('pg_restore --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d ' + probe))
                counts = execute(client, command + shlex.quote('psql -U "$POSTGRES_USER" -d ' + probe
                                 + ' -Atc "SELECT COUNT(*) FROM runs; SELECT COUNT(*) FROM models; SELECT COUNT(*) FROM invocations;"'))
                proof['isolated_restore_verified'] = True
                proof['restored_rows'] = dict(zip(['runs', 'models', 'invocations'], map(int, counts.split())))
            finally:
                execute(client, command + shlex.quote('dropdb -U "$POSTGRES_USER" ' + probe))
    (ROOT / '.platform/server-backup-report.json').write_text(json.dumps(proof, indent=2) + '\n')
    print(json.dumps(proof, indent=2))


if __name__ == '__main__':
    main()
