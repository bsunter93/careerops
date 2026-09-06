/* master.json + variant.json -> tailored .docx
   Variant controls: tagline, profile, skills lines, which bullets appear and in what order,
   and per-bullet label overrides. Content itself always comes from master.json. */
const { Document, Packer, Paragraph, TextRun, AlignmentType, BorderStyle,
        TabStopType, LevelFormat, Tab } = require("docx");
const fs = require("fs");

const master  = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const variant = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const F = "Calibri", RIGHT_TAB = 10656;
const S = Object.assign({ line: 242, after: 40, secBefore: 112, secAfter: 54,
                          top: 545, bottom: 430 }, variant.spacing || {});

const rule = () => ({ bottom: { style: BorderStyle.SINGLE, size: 6, color: "9A9A9A", space: 2 } });
const run  = (t, o = {}) => new TextRun(Object.assign({ text: t, size: 19, font: F }, o));
const body = () => ({ spacing: { after: S.after, line: S.line, lineRule: "auto" } });

const P = (children, extra = {}) => new Paragraph(Object.assign({ children }, extra));

const section = t => P([run(t, { bold: true, size: 21, color: "1F3864", characterSpacing: 20 })],
  { spacing: { before: S.secBefore, after: S.secAfter }, border: rule() });

const orgLine = (org, title, loc, dates) => P([
    run(org, { bold: true, size: 21 }),
    ...(title ? [run(`  |  ${title}`, { bold: true, size: 20, color: "1F3864" })] : []),
    run(`  |  ${loc}`, { color: "444444" }),
    new TextRun({ children: [new Tab()], size: 19, font: F }),
    run(dates, { bold: true }),
  ], { spacing: { before: 108, after: 40 }, tabStops: [{ type: TabStopType.RIGHT, position: RIGHT_TAB }] });

const roleLine = (title, dates) => P([
    run(title, { bold: true, italics: true, color: "1F3864" }),
    new TextRun({ children: [new Tab()], size: 19, font: F }),
    run(dates, { italics: true, color: "444444" }),
  ], { spacing: { before: 52, after: 26 }, indent: { left: 180 },
       tabStops: [{ type: TabStopType.RIGHT, position: RIGHT_TAB }] });

const bullet = (label, text, ind = 0) => P([run(`${label}: `, { bold: true }), run(text)],
  Object.assign({ numbering: { reference: "b", level: 0 },
                  indent: { left: 340 + ind, hanging: 160 } }, body()));

const skillLine = (label, items) => P([run(`${label}: `, { bold: true }), run(items.join(", "))],
  Object.assign({ indent: { left: 180 } }, body()));

const eduBullet = (lead, rest) => P([run(lead, { bold: true }), run(rest)],
  Object.assign({ numbering: { reference: "b", level: 0 },
                  indent: { left: 340, hanging: 160 } }, body()));

// ---- assemble ----
const keep = new Set(variant.bullets);
const rank = id => variant.bullets.indexOf(id);
const label = b => (variant.labels && variant.labels[b.id]) || b.label;
const pick = (arr, ind = 0) => arr.filter(b => keep.has(b.id))
  .sort((a, z) => rank(a.id) - rank(z.id))
  .map(b => bullet(label(b), b.text, ind));

const kids = [
  P([run(master.header.name, { bold: true, size: 40, characterSpacing: 20 })],
    { alignment: AlignmentType.CENTER, spacing: { after: 20 } }),
  P([run(master.header.contact, { size: 18, color: "333333" })],
    { alignment: AlignmentType.CENTER, spacing: { after: 60 } }),
  P([run(variant.tagline, { bold: true, size: 21, color: "1F3864", characterSpacing: 30 })],
    { alignment: AlignmentType.CENTER, spacing: { after: 100 } }),
  P([run(variant.profile)], Object.assign({ alignment: AlignmentType.JUSTIFIED }, body())),
  section("CORE COMPETENCIES"),
];
for (const k of ["strategy", "data", "leadership"]) {
  if (variant.skills[k] && variant.skills[k].length)
    kids.push(skillLine(master.skills[k].label, variant.skills[k]));
}
kids.push(section("PROFESSIONAL EXPERIENCE"));
for (const e of master.experience) {
  if (e.roles) {
    const any = e.roles.some(r => r.bullets.some(b => keep.has(b.id)));
    if (!any) continue;
    kids.push(orgLine(e.org, null, e.location, e.dates));
    for (const r of e.roles) {
      const bs = pick(r.bullets, 120);
      if (!bs.length) continue;
      kids.push(roleLine(r.title, r.dates), ...bs);
    }
  } else {
    const bs = pick(e.bullets);
    if (!bs.length) continue;
    kids.push(orgLine(e.org, e.title, e.location, e.dates), ...bs);
  }
}
kids.push(section("EDUCATION & CERTIFICATIONS"),
  ...master.education.map(x => eduBullet(x.lead, x.rest)));

const doc = new Document({
  numbering: { config: [{ reference: "b", levels: [{ level: 0, format: LevelFormat.BULLET,
    text: "•", alignment: AlignmentType.LEFT,
    style: { paragraph: { indent: { left: 340, hanging: 160 } } } }] }] },
  sections: [{ properties: { page: { size: { width: 12240, height: 15840 },
      margin: { top: S.top, bottom: S.bottom, left: 792, right: 792 } } }, children: kids }],
});
Packer.toBuffer(doc).then(b => { fs.writeFileSync(variant.out, b);
  console.log(`wrote ${variant.out} (${b.length} bytes, ${keep.size} bullets)`); });
