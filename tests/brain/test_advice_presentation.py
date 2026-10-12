import json
from pathlib import Path
import subprocess
import unittest


class AdvicePresentationTests(unittest.TestCase):
    def test_suggestion_is_escaped_and_expired_or_consumed_controls_are_disabled(self):
        page = Path('src/entries/web/templates/index.html').read_text()
        code = page[page.index('        const adviceActionLabels'):page.index('        let _prevAllDone')]
        fixture = {'recovery_confirmation_enabled': True, 'state': 'recovery_required',
                   'recovery_advice': {'advice_id': 'abc', 'action': 'continue', 'source': 'llm',
                    'applicable': True, 'summary': '<img src=x onerror=alert(1)>', 'reason': 'test'}}
        script = '''const assert=require('assert'); let taskControlPending=false;
function taskPhase(x){return x;}
function taskText(x){return String(x??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
''' + code + '\nconst data=' + json.dumps(fixture) + ''';
let html=renderRecoveryAdvice(data);
assert(html.includes('来源：LLM')); assert(html.includes('&lt;img')); assert(!html.includes('<img'));
assert(!html.includes('disabled'));
data.recovery_advice.applicable=false; assert(renderRecoveryAdvice(data).includes('disabled'));
data.recovery_advice.applicable=true;data.advice_confirmations=[{advice_id:'abc',action:'continue',status:'processing'}];
assert(renderRecoveryAdvice(data).includes('disabled'));assert(renderRecoveryAdvice(data).includes('处理结果待核对'));
data.recovery_confirmation_enabled=false;assert.equal(renderRecoveryAdvice(data),'');
'''
        subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
