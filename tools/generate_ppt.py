import os
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape


OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "PredictaMaint_Hackathon_Deck.pptx")

SLIDE_W = 12192000
SLIDE_H = 6858000

BG = "07111F"
PANEL = "10263F"
ACCENT = "5BE7FF"
ACCENT_2 = "FF9A57"
TEXT = "EDF6FF"
MUTED = "9CB1CC"
GREEN = "39F0A7"
RED = "FF6B7C"
YELLOW = "FFD36A"


def emu(value):
    return str(int(value))


def xml_text_paragraphs(lines, font_size=1800, color=TEXT, bold=False, bullet=False):
    paragraphs = []
    for line in lines:
        if bullet:
            ppr = '<a:pPr marL="342900" indent="-171450"><a:buChar char="•"/></a:pPr>'
        else:
            ppr = ""
        paragraphs.append(
            f"""
            <a:p>
              {ppr}
              <a:r>
                <a:rPr lang="en-US" sz="{font_size}" b="{1 if bold else 0}">
                  <a:solidFill><a:srgbClr val="{color}"/></a:solidFill>
                </a:rPr>
                <a:t>{escape(line)}</a:t>
              </a:r>
              <a:endParaRPr lang="en-US" sz="{font_size}">
                <a:solidFill><a:srgbClr val="{color}"/></a:solidFill>
              </a:endParaRPr>
            </a:p>
            """
        )
    return "".join(paragraphs)


def shape_rect(shape_id, name, x, y, w, h, text="", fill=PANEL, line=ACCENT, radius="rect",
               font_size=2000, color=TEXT, bold=False):
    tx = ""
    if text:
        tx = f"""
        <p:txBody>
          <a:bodyPr wrap="square" rtlCol="0" anchor="ctr"/>
          <a:lstStyle/>
          {xml_text_paragraphs(text.split("\\n"), font_size=font_size, color=color, bold=bold)}
        </p:txBody>
        """
    return f"""
    <p:sp>
      <p:nvSpPr>
        <p:cNvPr id="{shape_id}" name="{escape(name)}"/>
        <p:cNvSpPr/>
        <p:nvPr/>
      </p:nvSpPr>
      <p:spPr>
        <a:xfrm>
          <a:off x="{emu(x)}" y="{emu(y)}"/>
          <a:ext cx="{emu(w)}" cy="{emu(h)}"/>
        </a:xfrm>
        <a:prstGeom prst="{radius}"><a:avLst/></a:prstGeom>
        <a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>
        <a:ln w="19050">
          <a:solidFill><a:srgbClr val="{line}"/></a:solidFill>
        </a:ln>
      </p:spPr>
      {tx}
    </p:sp>
    """


def shape_text(shape_id, name, x, y, w, h, lines, font_size=2400, color=TEXT, bold=False, bullet=False):
    return f"""
    <p:sp>
      <p:nvSpPr>
        <p:cNvPr id="{shape_id}" name="{escape(name)}"/>
        <p:cNvSpPr txBox="1"/>
        <p:nvPr/>
      </p:nvSpPr>
      <p:spPr>
        <a:xfrm>
          <a:off x="{emu(x)}" y="{emu(y)}"/>
          <a:ext cx="{emu(w)}" cy="{emu(h)}"/>
        </a:xfrm>
        <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        <a:noFill/>
        <a:ln><a:noFill/></a:ln>
      </p:spPr>
      <p:txBody>
        <a:bodyPr wrap="square" rtlCol="0"/>
        <a:lstStyle/>
        {xml_text_paragraphs(lines, font_size=font_size, color=color, bold=bold, bullet=bullet)}
      </p:txBody>
    </p:sp>
    """


def arrow_text(shape_id, x, y, w, h, arrow="→", color=ACCENT):
    return shape_text(shape_id, f"Arrow {shape_id}", x, y, w, h, [arrow], font_size=2800, color=color, bold=True)


def slide_xml(shapes, bg=BG):
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:bg>
      <p:bgPr>
        <a:solidFill><a:srgbClr val="{bg}"/></a:solidFill>
        <a:effectLst/>
      </p:bgPr>
    </p:bg>
    <p:spTree>
      <p:nvGrpSpPr>
        <p:cNvPr id="1" name=""/>
        <p:cNvGrpSpPr/>
        <p:nvPr/>
      </p:nvGrpSpPr>
      <p:grpSpPr>
        <a:xfrm>
          <a:off x="0" y="0"/>
          <a:ext cx="0" cy="0"/>
          <a:chOff x="0" y="0"/>
          <a:chExt cx="0" cy="0"/>
        </a:xfrm>
      </p:grpSpPr>
      {shapes}
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>
"""


def slide_rels():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>
"""


def make_slides():
    slides = []

    slides.append(slide_xml(
        shape_text(2, "Kicker", 550000, 420000, 5000000, 400000, ["Hack Malenadu '26 | Industrial Intelligence"], 1600, ACCENT, True) +
        shape_text(3, "Title", 550000, 950000, 8600000, 1200000, ["PredictaMaint"], 3400, TEXT, True) +
        shape_text(4, "Subtitle", 550000, 2050000, 9200000, 700000, ["AI-Powered Predictive Maintenance Agent"], 2200, MUTED, False) +
        shape_rect(5, "Metric 1", 550000, 3200000, 2400000, 1000000, "4 Machines\nLive Monitored", fill="0D2035", line=ACCENT, radius="roundRect", font_size=2000, bold=True) +
        shape_rect(6, "Metric 2", 3200000, 3200000, 2400000, 1000000, "Real-Time\nSSE Dashboard", fill="0D2035", line=GREEN, radius="roundRect", font_size=2000, bold=True) +
        shape_rect(7, "Metric 3", 5850000, 3200000, 2400000, 1000000, "ML Risk\nScoring", fill="0D2035", line=YELLOW, radius="roundRect", font_size=2000, bold=True) +
        shape_rect(8, "Metric 4", 8500000, 3200000, 2400000, 1000000, "Auto Alert &\nMaintenance", fill="0D2035", line=ACCENT_2, radius="roundRect", font_size=2000, bold=True) +
        shape_text(9, "Footer", 550000, 5000000, 5000000, 300000, ["Team deck generated from project repo"], 1200, MUTED, False)
    ))

    slides.append(slide_xml(
        shape_text(2, "Title", 500000, 300000, 6000000, 500000, ["Problem & Opportunity"], 2600, TEXT, True) +
        shape_rect(3, "Problem", 500000, 1100000, 5100000, 4500000, "", fill="0E2238", line=RED, radius="roundRect") +
        shape_text(4, "Problem text", 800000, 1400000, 4500000, 3800000, [
            "Unexpected machine faults cause downtime, loss, and reactive maintenance.",
            "Operators need one view across all assets, not isolated machine screens.",
            "Transient spikes and gradual wear are both hard to detect early."
        ], 1900, TEXT, False, True) +
        shape_rect(5, "Opportunity", 6400000, 1100000, 5100000, 4500000, "", fill="0E2238", line=ACCENT, radius="roundRect") +
        shape_text(6, "Opportunity text", 6700000, 1400000, 4500000, 3800000, [
            "Score sensor anomalies in real time.",
            "Prioritize the riskiest machines first.",
            "Trigger alerts and auto-schedule maintenance before failure."
        ], 1900, TEXT, False, True)
    ))

    slides.append(slide_xml(
        shape_text(2, "Title", 500000, 300000, 6500000, 500000, ["Solution Snapshot"], 2600, TEXT, True) +
        shape_text(3, "Body", 500000, 1100000, 5600000, 4500000, [
            "Frontend: Industrial live dashboard in plain HTML/CSS/JS",
            "Backend: FastAPI for SSE streams, alerts, maintenance, and status",
            "ML: Per-machine Isolation Forest with dynamic baselines",
            "Agent: Autonomous prioritization, alert dispatch, and booking loop",
            "Data: 7-day historical sensor patterns with wear, spikes, and faults"
        ], 1900, TEXT, False, True) +
        shape_rect(4, "Highlight", 7000000, 1200000, 4000000, 3400000,
                   "What makes it hackathon-ready?\n\nSingle dashboard\nClear risk score\nPlain-English explanations\nReal-time operations flow",
                   fill="14304F", line=ACCENT_2, radius="roundRect", font_size=2000, bold=True)
    ))

    slides.append(slide_xml(
        shape_text(2, "Title", 500000, 300000, 7000000, 500000, ["System Architecture Flow"], 2600, TEXT, True) +
        shape_rect(3, "Box 1", 500000, 1600000, 1900000, 850000, "Sensor Streams\n4 Machines", fill="113251", line=ACCENT, radius="roundRect", font_size=1800, bold=True) +
        arrow_text(4, 2550000, 1820000, 400000, 300000) +
        shape_rect(5, "Box 2", 3100000, 1600000, 2300000, 850000, "Node Simulator\n/history + /stream", fill="113251", line=GREEN, radius="roundRect", font_size=1800, bold=True) +
        arrow_text(6, 5600000, 1820000, 400000, 300000) +
        shape_rect(7, "Box 3", 6150000, 1600000, 2300000, 850000, "FastAPI Backend\nscore + alert APIs", fill="113251", line=YELLOW, radius="roundRect", font_size=1800, bold=True) +
        arrow_text(8, 8650000, 1820000, 400000, 300000) +
        shape_rect(9, "Box 4", 9200000, 1600000, 2300000, 850000, "Dashboard UI\nlive risk board", fill="113251", line=ACCENT_2, radius="roundRect", font_size=1800, bold=True) +
        shape_rect(10, "Agent", 4100000, 3450000, 4000000, 1000000, "Autonomous Agent Loop\nprioritizes critical events and schedules maintenance", fill="0F2742", line=RED, radius="roundRect", font_size=1900, bold=True) +
        shape_text(11, "Caption", 1100000, 5200000, 9500000, 600000, ["Live data flows left to right. Decision automation sits on top of the backend and reacts to critical machine states."], 1700, MUTED)
    ))

    slides.append(slide_xml(
        shape_text(2, "Title", 500000, 300000, 7000000, 500000, ["ML Pipeline Flow"], 2600, TEXT, True) +
        shape_rect(3, "Step 1", 500000, 1500000, 1900000, 850000, "History CSV\n7-Day Sensor Data", fill="0F2742", line=ACCENT, radius="roundRect", font_size=1800, bold=True) +
        arrow_text(4, 2500000, 1710000, 300000, 300000) +
        shape_rect(5, "Step 2", 2900000, 1500000, 2100000, 850000, "Baselines\nmean/std/range", fill="0F2742", line=GREEN, radius="roundRect", font_size=1800, bold=True) +
        arrow_text(6, 5150000, 1710000, 300000, 300000) +
        shape_rect(7, "Step 3", 5550000, 1500000, 2100000, 850000, "Isolation Forest\nper machine", fill="0F2742", line=YELLOW, radius="roundRect", font_size=1800, bold=True) +
        arrow_text(8, 7800000, 1710000, 300000, 300000) +
        shape_rect(9, "Step 4", 8200000, 1500000, 3000000, 850000, "Risk Engine\nz-score + compound + drift", fill="0F2742", line=ACCENT_2, radius="roundRect", font_size=1800, bold=True) +
        shape_rect(10, "Output", 2800000, 3400000, 6500000, 1100000, "Output: risk_score, risk_level, explanation, alert decision", fill="113251", line=RED, radius="roundRect", font_size=2200, bold=True) +
        shape_text(11, "Caption", 1200000, 5000000, 9500000, 700000, ["This combines unsupervised anomaly detection with interpretable threshold-based reasoning, which is ideal for a hackathon demo."], 1700, MUTED)
    ))

    slides.append(slide_xml(
        shape_text(2, "Title", 500000, 300000, 7800000, 500000, ["Real-Time Alert Flow"], 2600, TEXT, True) +
        shape_rect(3, "A", 500000, 1650000, 1900000, 800000, "Incoming Reading", fill="113251", line=ACCENT, radius="roundRect", font_size=1800, bold=True) +
        arrow_text(4, 2550000, 1840000, 350000, 300000) +
        shape_rect(5, "B", 3050000, 1650000, 2000000, 800000, "Model Scoring", fill="113251", line=GREEN, radius="roundRect", font_size=1800, bold=True) +
        arrow_text(6, 5200000, 1840000, 350000, 300000) +
        shape_rect(7, "C", 5700000, 1650000, 2300000, 800000, "Critical / Warning?", fill="113251", line=YELLOW, radius="roundRect", font_size=1800, bold=True) +
        arrow_text(8, 8150000, 1840000, 350000, 300000) +
        shape_rect(9, "D", 8650000, 1650000, 2500000, 800000, "POST /alert", fill="113251", line=RED, radius="roundRect", font_size=1800, bold=True) +
        shape_text(10, "Down", 9300000, 2700000, 700000, 300000, ["↓"], 3000, ACCENT_2, True) +
        shape_rect(11, "E", 8400000, 3300000, 2800000, 900000, "Auto Maintenance Booking\nPOST /schedule-maintenance", fill="0F2742", line=ACCENT_2, radius="roundRect", font_size=1800, bold=True) +
        shape_text(12, "Caption", 900000, 5000000, 9800000, 700000, ["Alerts and maintenance can now be streamed live to the dashboard, making the decision loop visible in real time."], 1700, MUTED)
    ))

    slides.append(slide_xml(
        shape_text(2, "Title", 500000, 300000, 7200000, 500000, ["Results & Validation"], 2600, TEXT, True) +
        shape_rect(3, "Metric A", 650000, 1250000, 2450000, 1200000, "Offline Holdout Accuracy\n76.18%", fill="12304D", line=ACCENT, radius="roundRect", font_size=2200, bold=True) +
        shape_rect(4, "Metric B", 3200000, 1250000, 2450000, 1200000, "Precision\n89.13%", fill="12304D", line=GREEN, radius="roundRect", font_size=2200, bold=True) +
        shape_rect(5, "Metric C", 5750000, 1250000, 2450000, 1200000, "Recall\n59.00%", fill="12304D", line=YELLOW, radius="roundRect", font_size=2200, bold=True) +
        shape_rect(6, "Metric D", 8300000, 1250000, 2450000, 1200000, "F1 Score\n71.00%", fill="12304D", line=ACCENT_2, radius="roundRect", font_size=2200, bold=True) +
        shape_text(7, "Notes", 700000, 3200000, 10000000, 2200000, [
            "CNC_01 and CNC_02 show strong detection on labeled historical faults.",
            "PUMP_03 remains the hardest machine and is the main opportunity for improvement.",
            "Current evaluation is a time-based holdout, not a production benchmark."
        ], 1900, TEXT, False, True)
    ))

    slides.append(slide_xml(
        shape_text(2, "Title", 500000, 300000, 7600000, 500000, ["Demo Flow & Closing"], 2600, TEXT, True) +
        shape_text(3, "Body", 600000, 1200000, 5800000, 4200000, [
            "1. Start Node simulator",
            "2. Start FastAPI backend",
            "3. Start autonomous agent loop",
            "4. Open dashboard and show live machine ranking",
            "5. Trigger alert and auto-maintenance in real time"
        ], 2000, TEXT, False, True) +
        shape_rect(4, "Close", 7100000, 1400000, 3900000, 2600000,
                   "Why this stands out\n\nReal-time UI\nExplainable scoring\nPriority queue\nOperational action, not just prediction",
                   fill="14304F", line=ACCENT, radius="roundRect", font_size=2000, bold=True) +
        shape_text(5, "Footer", 600000, 5600000, 5000000, 300000, ["PredictaMaint | AI-powered preventive decisions for industrial assets"], 1500, MUTED)
    ))

    return slides


def content_types(slide_count):
    slide_overrides = "\n".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, slide_count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
  <Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>
  <Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>
  <Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  {slide_overrides}
</Types>
"""


def root_rels():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""


def app_props(slide_count):
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
            xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>OpenAI Codex</Application>
  <PresentationFormat>Widescreen</PresentationFormat>
  <Slides>{slide_count}</Slides>
  <Notes>0</Notes>
  <HiddenSlides>0</HiddenSlides>
  <MMClips>0</MMClips>
  <ScaleCrop>false</ScaleCrop>
  <HeadingPairs>
    <vt:vector size="2" baseType="variant">
      <vt:variant><vt:lpstr>Slides</vt:lpstr></vt:variant>
      <vt:variant><vt:i4>{slide_count}</vt:i4></vt:variant>
    </vt:vector>
  </HeadingPairs>
  <TitlesOfParts>
    <vt:vector size="{slide_count}" baseType="lpstr">
      {''.join('<vt:lpstr>Slide</vt:lpstr>' for _ in range(slide_count))}
    </vt:vector>
  </TitlesOfParts>
</Properties>
"""


def core_props():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:dcterms="http://purl.org/dc/terms/"
                   xmlns:dcmitype="http://purl.org/dc/dcmitype/"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>PredictaMaint Hackathon Deck</dc:title>
  <dc:creator>OpenAI Codex</dc:creator>
  <cp:lastModifiedBy>OpenAI Codex</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>
"""


def presentation_xml(slide_count):
    sld_ids = "\n".join(
        f'<p:sldId id="{255 + i}" r:id="rId{1 + i}"/>' for i in range(1, slide_count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                saveSubsetFonts="1" autoCompressPictures="0">
  <p:sldMasterIdLst>
    <p:sldMasterId id="2147483648" r:id="rId1"/>
  </p:sldMasterIdLst>
  <p:sldIdLst>
    {sld_ids}
  </p:sldIdLst>
  <p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}"/>
  <p:notesSz cx="6858000" cy="9144000"/>
  <p:defaultTextStyle/>
</p:presentation>
"""


def presentation_rels(slide_count):
    rels = ['<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>']
    for i in range(1, slide_count + 1):
        rels.append(
            f'<Relationship Id="rId{i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>'
        )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  {"".join(rels)}
</Relationships>
"""


def slide_master_xml():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
             xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
             xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld name="Master">
    <p:bg>
      <p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef>
    </p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    </p:spTree>
  </p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst>
    <p:sldLayoutId id="2147483649" r:id="rId1"/>
  </p:sldLayoutIdLst>
  <p:txStyles>
    <p:titleStyle/>
    <p:bodyStyle/>
    <p:otherStyle/>
  </p:txStyles>
</p:sldMaster>
"""


def slide_master_rels():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>
"""


def slide_layout_xml():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
             xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
             xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
             type="blank" preserve="1">
  <p:cSld name="Blank">
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>
"""


def slide_layout_rels():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>
"""


def theme_xml():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="PredictaMaint Theme">
  <a:themeElements>
    <a:clrScheme name="PredictaMaint">
      <a:dk1><a:srgbClr val="07111F"/></a:dk1>
      <a:lt1><a:srgbClr val="EDF6FF"/></a:lt1>
      <a:dk2><a:srgbClr val="10263F"/></a:dk2>
      <a:lt2><a:srgbClr val="D9E8F5"/></a:lt2>
      <a:accent1><a:srgbClr val="5BE7FF"/></a:accent1>
      <a:accent2><a:srgbClr val="39F0A7"/></a:accent2>
      <a:accent3><a:srgbClr val="FFD36A"/></a:accent3>
      <a:accent4><a:srgbClr val="FF9A57"/></a:accent4>
      <a:accent5><a:srgbClr val="FF6B7C"/></a:accent5>
      <a:accent6><a:srgbClr val="9CB1CC"/></a:accent6>
      <a:hlink><a:srgbClr val="5BE7FF"/></a:hlink>
      <a:folHlink><a:srgbClr val="FF9A57"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="PredictaMaint Fonts">
      <a:majorFont><a:latin typeface="Aptos Display"/></a:majorFont>
      <a:minorFont><a:latin typeface="Aptos"/></a:minorFont>
    </a:fontScheme>
    <a:fmtScheme name="PredictaMaint Format">
      <a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>
      <a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>
      <a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>
      <a:bgFillStyleLst><a:solidFill><a:schemeClr val="dk1"/></a:solidFill></a:bgFillStyleLst>
    </a:fmtScheme>
  </a:themeElements>
  <a:objectDefaults/>
  <a:extraClrSchemeLst/>
</a:theme>
"""


def fixed_xml(path):
    if path.endswith("presProps.xml"):
        return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentationPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
                  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                  xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>"""
    if path.endswith("viewProps.xml"):
        return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:viewPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
          xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:normalViewPr/>
  <p:slideViewPr/>
  <p:outlineViewPr/>
  <p:notesTextViewPr/>
  <p:sorterViewPr/>
</p:viewPr>"""
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>"""


def build_pptx():
    slides = make_slides()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with zipfile.ZipFile(OUT_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types(len(slides)))
        zf.writestr("_rels/.rels", root_rels())
        zf.writestr("docProps/app.xml", app_props(len(slides)))
        zf.writestr("docProps/core.xml", core_props())
        zf.writestr("ppt/presentation.xml", presentation_xml(len(slides)))
        zf.writestr("ppt/_rels/presentation.xml.rels", presentation_rels(len(slides)))
        zf.writestr("ppt/slideMasters/slideMaster1.xml", slide_master_xml())
        zf.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", slide_master_rels())
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", slide_layout_xml())
        zf.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", slide_layout_rels())
        zf.writestr("ppt/theme/theme1.xml", theme_xml())
        zf.writestr("ppt/presProps.xml", fixed_xml("presProps.xml"))
        zf.writestr("ppt/viewProps.xml", fixed_xml("viewProps.xml"))
        zf.writestr("ppt/tableStyles.xml", fixed_xml("tableStyles.xml"))
        for i, slide in enumerate(slides, start=1):
            zf.writestr(f"ppt/slides/slide{i}.xml", slide)
            zf.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", slide_rels())
    print(OUT_PATH)


if __name__ == "__main__":
    build_pptx()
