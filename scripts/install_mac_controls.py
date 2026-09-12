"""Install local launchers without registering an always-running login service."""
from pathlib import Path
import shlex

root = Path(__file__).resolve().parents[1]
dest = Path.home() / 'Desktop/Internship Radar Controls'
dest.mkdir(exist_ok=True)
for name, action in [('Start Radar Auto Apply', 'start'), ('Stop Radar Auto Apply', 'stop'),
                     ('Check Radar Worker', 'status'), ('Connect Radar Worker', 'connect')]:
    path = dest / (name + '.command')
    path.write_text('#!/bin/zsh\ncd ' + shlex.quote(str(root)) + '\n' +
        shlex.quote(str(root / '.venv/bin/python')) + ' -m applicant.manager ' + action +
        '\nread "?Press Enter to close."\n')
    path.chmod(0o700)
print('Installed Mac controls:', dest)
