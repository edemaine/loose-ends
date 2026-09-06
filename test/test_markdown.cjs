// Run with: pnpm --dir test install --frozen-lockfile && pnpm --dir test test
const assert = require("node:assert/strict");
const { test, before } = require("node:test");
require("../src/workbench_web/review_model.js");

let renderer;
before(async () => {
  renderer = globalThis.LooseEndsReviewModel.createMarkdownRenderer({
    markdownit: require("markdown-it"),
    katex: require("katex"),
    mdItPluginKatex: await import("@mdit/plugin-katex"),
  });
});
const abstract = source => renderer.render(source, { latexProse: true });

test("converts LaTeX prose, including nested formatting and accents", () => {
  assert.equal(abstract("A \\emph{concise \\textbf{nested}} abstract by Gourv\\`es, Erd\\H{o}s, Fran\\c{c}ois, and a na\\\"ive coauthor. \\ae{}~\\o{} ``quoted''."),
    '<p>A <em>concise <strong>nested</strong></em> abstract by Gourvès, Erdős, François, and a naïve coauthor. æ\u00a0ø “quoted”.</p>\n');
});

test("leaves math contents identical to the ordinary renderer", () => {
  for (const math of [
    String.raw`\(\#\mathrm P\)`, String.raw`$\textbf{A}~\text{\%\&\_\#} + \hat{x}$`,
    String.raw`\[\textit{A}~\tilde{x}\]`, "$$\\text{``quotes''}~\\dot{x}$$",
  ]) {
    assert.equal(abstract(math), renderer.render(math));
    assert.doesNotMatch(abstract(math), /katex-error/);
    assert.match(abstract(math), /class="katex/);
  }
});

test("preserves code spans, fenced code, and link destinations", () => {
  for (const code of ["`~ \\emph{x} ''`", "``~ `x` \\ae``", "~~~tex\n~ \\emph{x} ''\n~~~"]) {
    assert.equal(abstract(code), renderer.render(code));
  }
  assert.match(abstract('[A~B](https://example.org/~user)'), /href="https:\/\/example.org\/~user"/);
  assert.match(abstract('[A~B](https://example.org/~user)'), /A\u00a0B/);
  assert.equal(abstract(String.raw`\texttt{~ \emph{x}}`), '<p><code>~ \\emph{x}</code></p>\n');
});

test("leaves ordinary Markdown and escaped punctuation alone", () => {
  const source = String.raw`\emph{x} A~B \# \% \& \_`;
  assert.equal(renderer.render(source), '<p>\\emph{x} A~B # % &amp; _</p>\n');
  assert.equal(abstract(String.raw`\# \% \& \_ \~`), '<p># % &amp; _ ~</p>\n');
  assert.equal(renderer.render(source), '<p>\\emph{x} A~B # % &amp; _</p>\n');
});

test("handles escaped braces and leaves unsupported or incomplete commands visible", () => {
  assert.equal(abstract(String.raw`\emph{a \{b\} c}`), '<p><em>a {b} c</em></p>\n');
  assert.equal(abstract(String.raw`\emph{unfinished`), '<p>\\emph{unfinished</p>\n');
  assert.equal(abstract(String.raw`\unknown{a} \ref{x}`), '<p>\\unknown{a} \\ref{x}</p>\n');
  assert.equal(abstract(String.raw`*a \emph{b **c**} d*`), '<p><em>a <em>b <strong>c</strong></em> d</em></p>\n');
  assert.equal(abstract(String.raw`\textbf{\(\#\mathrm P\)}~complete`),
    '<p><strong>' + renderer.renderInline(String.raw`\(\#\mathrm P\)`) + '</strong>\u00a0complete</p>\n');
});
