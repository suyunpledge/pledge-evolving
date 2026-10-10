"""Bounded, read-only previews of mediated file tool calls; never execute code."""
import copy
import difflib
import hashlib
import json
from pathlib import Path
from forge.secrets import assert_public_path, read_public_bytes

MUTATIONS = {'write_file','edit_file','apply_patch','delete_file','edit_config'}


def prepare_change(name, arguments, workspace, scope):
    if name not in MUTATIONS or name == 'edit_config' and arguments.get('patch') is None:
        return None
    root = Path(workspace).resolve()
    args = copy.deepcopy(arguments)
    blocks = args.get('patches') or args.get('edits') or []
    locations = blocks if name == 'apply_patch' else [args]
    if not isinstance(locations,list) or not locations or len(locations)>40:
        raise ValueError('Invalid or oversized change set')
    revisions, previews, buffers = {}, [], {}
    for block in locations:
        if not isinstance(block,dict) or not block.get('path'): raise ValueError('Change requires a path')
        candidate = Path(block['path'])
        path = (candidate if candidate.is_absolute() else root/candidate).resolve()
        if not path.is_relative_to(root): raise PermissionError('Change escapes the selected workspace')
        assert_public_path(path)
        block['path'] = str(path)
        if str(path) not in revisions:
            raw = read_public_bytes(path,limit=512*1024) if path.exists() else None
            revisions[str(path)] = hashlib.sha256(raw).hexdigest() if raw is not None else None
            # Match the core text tools' universal-newline reads, retaining BOM.
            # Windows CRLF files must accept the same multi-line anchors.
            buffers[str(path)] = raw.decode('utf-8').replace('\r\n','\n').replace('\r','\n') if raw is not None else ''
        original = buffers[str(path)]
        if name == 'write_file': updated = (original if args.get('append') else '')+str(args.get('content',''))
        elif name in {'edit_file','apply_patch'}:
            old, new = block.get('old'), str(block.get('new',''))
            if old:
                count = original.count(old)
                if count == 0 or count>1 and not block.get('replace_all'):
                    raise ValueError('Change anchor is missing or ambiguous')
                updated = original.replace(old,new) if block.get('replace_all') else original.replace(old,new,1)
            elif name == 'edit_file':
                lines = original.splitlines(keepends=True)
                start,end = args.get('start_line',1),args.get('end_line',len(lines))
                if any(not isinstance(v,int) or isinstance(v,bool) for v in (start,end)) or not 1<=start<=end<=len(lines):
                    raise ValueError('Invalid line range')
                updated = ''.join(lines[:start-1])+new+('' if not new or new.endswith('\n') else '\n')+''.join(lines[end:])
            else: raise ValueError('Patch requires an anchor')
        elif name == 'delete_file': updated = ''
        else:
            previews.append(str(path)+'\n'+json.dumps(scope.protect(args),ensure_ascii=False,indent=2))
            continue  # structured config validator resolves refs at execution
        if len(updated.encode('utf-8'))>512*1024: raise ValueError('Change preview exceeds 512 KiB')
        buffers[str(path)] = updated
        previews.append(''.join(difflib.unified_diff(scope.protect_text(original).splitlines(True),
                       scope.protect_text(updated).splitlines(True),fromfile=str(path),tofile=str(path)+' (proposed)')))
    args['_expected_revisions'] = revisions
    preview='\n'.join(previews)
    if len(preview)>120000: raise ValueError('Change exceeds the complete preview limit; split the edit')
    return {'name':name,'arguments':args,'preview':preview, 'workspace':str(root)}


def check_revisions(revisions):
    if not isinstance(revisions,dict) or len(revisions)>40: raise ValueError('Invalid revision guard')
    for target, expected in revisions.items():
        path = Path(target)
        raw = read_public_bytes(path,limit=512*1024) if path.exists() else None
        actual = hashlib.sha256(raw).hexdigest() if raw is not None else None
        if actual != expected: raise ValueError('File changed since preview; regenerate the change')
