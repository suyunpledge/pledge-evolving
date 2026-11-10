"""Real execution outcomes, exact retained output and safe read pagination."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from forge.execution import ActionExecutor,ActionRequest,create_execution_context
from forge.policy import Policy,Mode,Sandbox,ToolEffect
from forge.tools import build_builtin_registry,ToolSpec


class CommandFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def context(self,command='',**policy):
        registry=build_builtin_registry()
        shell=next(s for s in registry.all_specs() if s.name=='shell_exec')
        # The protected executor forbids arbitrary shell commands. This trusted
        # fixture runner binds a fixed host command, as the live challenge does.
        registry.register(ToolSpec('run_fixture_checks','Fixed fixture command',
            lambda args,ctx:shell.handler({'command':command},ctx),read_only=False,
            result_fields=shell.result_fields),trusted=True,effect=ToolEffect.NATIVE)
        ctx=create_execution_context(home=self.root/'home',workspace=self.root,
            registry=registry,policy=Policy(workspace=self.root,mode=Mode.ACCEPT_EDITS,
                sandbox=Sandbox.WORKSPACE_WRITE,allow=('run_fixture_checks',),non_interactive=True,**policy))
        self.addCleanup(ctx.secret_scope.close)
        return ctx

    def test_failed_command_keeps_status_and_exact_output_retrievable(self):
        command=f'"{sys.executable}" -c "import sys; print(\'FAIL: billing_case\'); print(\'z\'*7000); sys.exit(1)"'
        ctx=self.context(command);executor=ActionExecutor()
        result=executor.execute(ctx,ActionRequest('run_fixture_checks',{}))
        self.assertEqual(result.outcome,'failed')
        self.assertEqual(result.execution_state,'completed')
        self.assertEqual(result.meta['exit'],1)
        view=json.loads(result.content)
        self.assertEqual(view['summary']['exit_code'],1)
        self.assertTrue(view['summary']['nonzero_exit'])
        self.assertIn('FAIL: billing_case',view['preview'])
        identifier=view['_retained']['id'];parts=[];offset=0
        while offset is not None:
            page=executor.execute(ctx,ActionRequest('artifact_read',{'id':identifier,'offset':offset,'limit':4000}))
            self.assertEqual(page.outcome,'succeeded')
            self.assertTrue(page.meta['limit_capped'])
            self.assertLessEqual(page.meta['effective_limit'],3000)
            header,text=page.content.split('\n',1);parts.append(text)
            offset=json.loads(header)['next_offset']
        self.assertEqual(''.join(parts),'FAIL: billing_case\n'+'z'*7000+'\n')

    def test_invalid_pages_and_denied_reads_are_not_repaired(self):
        ctx=self.context(f'"{sys.executable}" -c "print(\'v\'*5000)"');executor=ActionExecutor()
        # Use a real output to obtain an authentic handle.
        result=executor.execute(ctx,ActionRequest('run_fixture_checks',{}))
        identifier=json.loads(result.content)['_retained']['id']
        for limit in (True,0,-1,2.5,'4000'):
            page=executor.execute(ctx,ActionRequest('artifact_read',{'id':identifier,'limit':limit}))
            self.assertEqual(page.outcome,'failed')
        denied=self.context(deny=('artifact_read',))
        page=executor.execute(denied,ActionRequest('artifact_read',{'id':identifier,'limit':4000}))
        self.assertEqual(page.error_code,'policy_denied')


if __name__=='__main__':unittest.main()
