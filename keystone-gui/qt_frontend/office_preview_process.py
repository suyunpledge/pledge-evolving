"""Single-use, windowless Qt WebEngine print worker. All document text stays local."""
import base64
import json
from pathlib import Path
import sys

from PySide6.QtCore import QBuffer, QIODevice, QMarginsF, QSize, QSizeF, QTimer, QUrl
from PySide6.QtGui import QGuiApplication, QPageLayout, QPageSize
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings


def main():
    state, number = json.load(sys.stdin.buffer)
    slides = state['kind'] == 'presentation'
    if slides and number > len(state['slideOrder']):
        raise ValueError('幻灯片页码超出范围。')
    app = QGuiApplication(sys.argv[:1])
    page = QWebEnginePage()
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    error = ['渲染超时。']
    result = []

    def printed(raw):
        try:
            buffer = QBuffer()
            buffer.setData(raw)
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            pdf = QPdfDocument()
            pdf.load(buffer)
            count = pdf.pageCount()
            if not count or (not slides and number > count):
                raise ValueError('无可渲染的页面或页码超出范围。')
            size = QSize(960, 540) if slides else QSize(794, 1123)
            bitmap = pdf.render(0 if slides else number - 1, size)
            out = QBuffer()
            out.open(QIODevice.OpenModeFlag.WriteOnly)
            if bitmap.isNull() or not bitmap.save(out, 'PNG'):
                raise ValueError('PNG 渲染失败。')
            result.append({'pages': len(state['slideOrder']) if slides else count,
                           'image': base64.b64encode(out.data().data()).decode('ascii')})
        except Exception as exc:
            error[0] = str(exc)
        app.quit()

    def composed(ok):
        if ok is not True:
            error[0] = '办公页面未能完成布局。'
            app.quit()
            return
        paper = QPageSize(QSizeF(254, 142.875), QPageSize.Unit.Millimeter) if slides else QPageSize(QPageSize.PageSizeId.A4)
        margin = 0 if slides else 18
        layout = QPageLayout(paper, QPageLayout.Orientation.Portrait, QMarginsF(margin, margin, margin, margin))
        page.printToPdf(printed, layout)

    def loaded(ok):
        if ok:
            page.runJavaScript('renderOffice(' + json.dumps(state, ensure_ascii=True) + ',' + str(number) + ')', 0, composed)
        else:
            error[0] = '无法打开本机办公渲染页面。'
            app.quit()

    page.loadFinished.connect(loaded)
    page.load(QUrl.fromLocalFile(str(Path(__file__).parent / 'office' / 'preview.html')))
    QTimer.singleShot(15000, app.quit)
    app.exec()
    if not result:
        raise ValueError(error[0])
    sys.stdout.write(json.dumps(result[0]))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        sys.stdout.write(json.dumps({'error': str(exc)}))
        sys.exit(1)
