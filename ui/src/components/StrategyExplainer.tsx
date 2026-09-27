const EXPLANATIONS: Record<string, string> = {
  fixed: 'Splits your document into equal-sized pieces by character count. Simple and fast, but may cut sentences in the middle. Best for structured data like CSV or JSON.',
  overlap: 'Like Fixed Size, but each piece shares some text with the next one. This helps the system find answers that fall near a boundary. A good general-purpose choice.',
  language: 'Splits at natural sentence and paragraph breaks before falling back to character count. Keeps sentences intact. Recommended for most narrative documents.',
  context_aware: "Uses the document's own structure — headings, paragraphs, tables — to define boundaries. Best for structured reports, policies, or manuals with clear section headings.",
  semantic: 'Groups sentences that are about the same topic together, regardless of their position. Produces the most meaningful chunks but is the slowest option. Best for long, dense documents.',
}

export default function StrategyExplainer({ strategy }: { strategy: string }) {
  const text = EXPLANATIONS[strategy]
  if (!text) return null
  return (
    <div className="mt-2 p-3 bg-blue-50 border border-blue-200 rounded text-sm text-blue-800">
      {text}
    </div>
  )
}
