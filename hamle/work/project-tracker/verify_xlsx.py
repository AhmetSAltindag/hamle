from pathlib import Path
import json
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
DATA = json.loads((ROOT / 'project-plan.json').read_text())
OUTPUT = ROOT.parent.parent / 'outputs/hamle-yeni-mac-devir-2026-10-10/Hamle_Proje_Takip_2026-10-10_Yeni_Mac_Devir.xlsx'
NATIVE = ROOT / 'xlsx-qa-handoff/native/Hamle_Proje_Takip_2026-10-10_Yeni_Mac_Devir.xlsx'
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}

def inspect_file(path):
    with zipfile.ZipFile(path) as z:
        sheets = [ET.fromstring(z.read(f'xl/worksheets/sheet{i}.xml')) for i in [1, 2, 3, 4]]
        cells = [{c.attrib['r']: c for c in sheet.findall('.//s:sheetData/s:row/s:c', NS)} for sheet in sheets]
        def value(sheet, ref):
            c = cells[sheet][ref]
            v = c.find('s:v', NS)
            return float(v.text) if v is not None and v.text else None
        metrics = {c: value(0, c) for c in ['B4', 'B6', 'D6', 'F6', 'H6', 'C23', 'D23', 'F23', 'G23', 'H23']}
        d=DATA['currentSummary']
        expected = {'B4': 46305, 'B6': d['completedWeight']/100, 'D6': d['remainingWeight']/100, 'F6': d['remainingTasks'], 'H6': d['completedTasks'], 'C23': 100, 'D23': d['completedWeight'], 'F23': d['remainingWeight'], 'G23': d['remainingTasks'], 'H23': d['completedTasks']}
        assert metrics == expected, (path.name, metrics)
        errors = [c.attrib['r'] for sheet in cells for c in sheet.values() if c.attrib.get('t') == 'e']
        assert not errors, errors
        panes = [s.find('.//s:pane', NS).attrib if s.find('.//s:pane', NS) is not None else None for s in sheets]
        for p in panes[1:3]:
            assert float(p['xSplit']) == 2 and float(p['ySplit']) == 6, panes
        tables = []
        for name in z.namelist():
            if name.startswith('xl/tables/table') and name.endswith('.xml'):
                t = ET.fromstring(z.read(name))
                tables.append({'name': t.attrib.get('name'), 'ref': t.attrib.get('ref'), 'filter': t.find('s:autoFilter', NS) is not None})
        assert any(t['ref'] == 'A6:N71' for t in tables), tables
        assert any(t['ref'] == 'A6:J20' for t in tables), tables
        assert all(t['filter'] for t in tables), tables
        return {'metrics': metrics, 'formula_errors': errors, 'panes': panes, 'tables': tables, 'file_size': path.stat().st_size}

report = {'artifact_export': inspect_file(OUTPUT), 'bundled_libreoffice_roundtrip': inspect_file(NATIVE), 'native_excel_interactive_tested': False}
(ROOT / 'xlsx-qa-handoff/export-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False, indent=2))
