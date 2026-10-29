import Editor, { loader, type OnMount } from "@monaco-editor/react";
import * as monaco from "monaco-editor/esm/vs/editor/editor.api.js";
import "monaco-editor/esm/vs/editor/edcore.main.js"; // find, comment toggle, bracket matching...
import "monaco-editor/esm/vs/basic-languages/python/python.contribution.js";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker.js?worker";
import { useEffect, useRef } from "react";

// Bundle Monaco (Python highlighting only) instead of loading it from a CDN.
self.MonacoEnvironment = { getWorker: () => new EditorWorker() };
loader.config({ monaco });

type Props = {
  value: string;
  onChange: (value: string) => void;
  dark: boolean;
  readOnly?: boolean;
  highlightLine?: number | null; // the line named by a level 5 hint
};

export default function CodeEditor({ value, onChange, dark, readOnly, highlightLine }: Props) {
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const decorations = useRef<monaco.editor.IEditorDecorationsCollection | null>(null);

  const onMount: OnMount = (editor) => {
    editorRef.current = editor as unknown as monaco.editor.IStandaloneCodeEditor;
    decorations.current = editorRef.current.createDecorationsCollection();
  };

  useEffect(() => {
    const editor = editorRef.current;
    if (!editor || !decorations.current) return;
    if (!highlightLine) {
      decorations.current.clear();
      return;
    }
    decorations.current.set([
      {
        range: new monaco.Range(highlightLine, 1, highlightLine, 1),
        options: { isWholeLine: true, className: "line-highlight" },
      },
    ]);
    editor.revealLineInCenterIfOutsideViewport(highlightLine);
  }, [highlightLine, value]);

  return (
    <Editor
      language="python"
      value={value}
      theme={dark ? "vs-dark" : "vs"}
      onChange={(v) => onChange(v ?? "")}
      onMount={onMount}
      options={{
        readOnly,
        minimap: { enabled: false },
        fontSize: 14,
        scrollBeyondLastLine: false,
        automaticLayout: true,
        tabSize: 4,
        padding: { top: 12 },
      }}
    />
  );
}
