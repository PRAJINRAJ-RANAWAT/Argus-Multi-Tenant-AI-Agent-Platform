import ReactMarkdown from "react-markdown"

// Reviews end with a GitHub-only "<sub>…</sub>" footer; show it as emphasis instead of
// printing the tags (raw HTML is never rendered).
const githubSub = /<sub>([^<]*)<\/sub>/g

/** Renders review and answer Markdown. Raw HTML is not rendered (react-markdown
 *  escapes it by default), and links open in a new tab without referrer. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="space-y-3 text-sm leading-relaxed [&_code]:rounded-sm [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[12px] [&_h2]:display [&_h2]:text-base [&_h3]:font-semibold [&_li]:ml-4 [&_li]:list-disc [&_li]:pl-1 [&_blockquote]:border-l-2 [&_blockquote]:border-warning [&_blockquote]:bg-warning/10 [&_blockquote]:px-3 [&_blockquote]:py-2 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:bg-muted [&_pre]:p-3">
      <ReactMarkdown
        components={{
          a: ({ href, children: c }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer noopener"
              className="underline decoration-neon decoration-2 underline-offset-2"
            >
              {c}
            </a>
          ),
        }}
      >
        {children.replace(githubSub, "*$1*")}
      </ReactMarkdown>
    </div>
  )
}
