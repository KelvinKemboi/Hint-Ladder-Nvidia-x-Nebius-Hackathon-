// Token-level diff of two Python reprs, for the side-by-side reveal.

export type Piece = { text: string; changed: boolean };

const TOKEN = /-?\d+(?:\.\d+)?|[A-Za-z_]\w*|'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|\s+|./g;

function tokens(text: string): string[] {
  return text.match(TOKEN) ?? [];
}

/** Each side split into pieces, with the tokens outside the longest common subsequence
 *  marked as changed. Whitespace only counts inside a changed run. */
export function diff(a: string, b: string): [Piece[], Piece[]] {
  const x = tokens(a);
  const y = tokens(b);
  const n = x.length;
  const m = y.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = x[i] === y[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const keepX = new Array<boolean>(n).fill(false);
  const keepY = new Array<boolean>(m).fill(false);
  for (let i = 0, j = 0; i < n && j < m; ) {
    if (x[i] === y[j]) {
      keepX[i++] = true;
      keepY[j++] = true;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      i++;
    } else {
      j++;
    }
  }
  return [merge(x, keepX), merge(y, keepY)];
}

function merge(toks: string[], keep: boolean[]): Piece[] {
  const pieces: Piece[] = [];
  const changedAt = (i: number) => i >= 0 && i < toks.length && !keep[i] && toks[i].trim() !== "";
  toks.forEach((text, i) => {
    // Whitespace inside a changed run is part of it, so the highlight isn't split.
    const changed =
      text.trim() === "" ? changedAt(i - 1) && changedAt(i + 1) : !keep[i];
    const last = pieces[pieces.length - 1];
    if (last && last.changed === changed) last.text += text;
    else pieces.push({ text, changed });
  });
  return pieces;
}
