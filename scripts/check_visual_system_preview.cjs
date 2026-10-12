/* Local-only browser checks/rendering; accepts bundled Playwright and Chrome paths. */
const path = require('path');
const fs = require('fs');
const assert = require('assert/strict');
const {pathToFileURL} = require('url');
const {chromium} = require(path.join(process.argv[2], 'playwright'));
const root = path.resolve(__dirname, '..');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.argv[3]});
  const page = await browser.newPage({viewport:{width:1360,height:850}, deviceScaleFactor:1});
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(pathToFileURL(path.join(root, 'reports/forge-visual-system-2-preview.html')).href);
    await page.screenshot({path:path.join(root,'reports/forge-visual-system-2-work.png')});
    await page.evaluate(() => {window.originalWork=document.querySelector('[data-workspace=work]');window.originalCode=document.querySelector('[data-buffer=runner]');});
    const workDraft=page.getByRole('textbox',{name:'Work Mode 草稿'});
    await workDraft.fill('Work 模式未保存草稿');
    await page.locator('[data-mode=coding]').click();
    await page.screenshot({path:path.join(root,'reports/forge-visual-system-2-coding.png')});
    await page.getByRole('textbox',{name:'runner.py 示例草稿'}).fill('# 未保存代码草稿\n');
    await page.locator('[data-file=models]').click();
    await page.locator('[data-mode=work]').click();
    assert.equal(await workDraft.inputValue(),'Work 模式未保存草稿');
    const before=Number(await page.locator('#heartbeat').textContent());
    await page.waitForFunction(value=>Number(document.querySelector('#heartbeat').textContent)>value,before);
    await page.locator('[data-mode=coding]').click();
    assert.equal(await page.locator('[data-file=models]').getAttribute('aria-selected'),'true');
    await page.locator('[data-file=runner]').click();
    assert.equal(await page.getByRole('textbox',{name:'runner.py 示例草稿'}).inputValue(),'# 未保存代码草稿\n');
    assert(await page.evaluate(()=>window.originalWork===document.querySelector('[data-workspace=work]')&&window.originalCode===document.querySelector('[data-buffer=runner]')));
    await page.locator('button[data-preset=editor-focus]').click();
    assert.equal(await page.locator('.ai-panel').isVisible(),false);
    await page.locator('button[data-preset=review]').click();
    assert.equal(await page.locator('[data-dock-content=diff]').isVisible(),true);
    await page.locator('button[data-preset=balanced]').click();
    const separator=page.locator('[data-side=files]');
    await separator.focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await separator.getAttribute('aria-valuenow'),'230');
    for(const width of [920,1100,1360,1920]){
      await page.setViewportSize({width,height:850});
      for(const mode of ['work','coding']){
        await page.locator(`[data-mode=${mode}]`).click();
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
      }
    }
    assert.deepEqual(errors,[]);
    const report={local_only:true,retained_drafts:true,retained_dom:true,retained_tabs:true,
      background_callback_continues:true,layout_presets:true,keyboard_panel_resize:true,
      viewport_widths:[920,1100,1360,1920],page_errors:errors,
      screenshots:'HTML layout specimens, not production Forge or native Tk screenshots'};
    fs.writeFileSync(path.join(root,'reports/forge-visual-system-2-preview-check.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
