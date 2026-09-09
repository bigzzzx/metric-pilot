import React from 'react';

function inlineParts(text) {
  return String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean).map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**')) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith('`') && part.endsWith('`')) return <code key={index}>{part.slice(1, -1)}</code>;
    return <React.Fragment key={index}>{part}</React.Fragment>;
  });
}

/** Safe, dependency-free rendering for the small Markdown subset emitted by chat. */
export default function MessageText({ children }) {
  const lines = String(children || '').split('\n');
  return <div className="message-text">{lines.map((line, index) => <React.Fragment key={index}>
    {inlineParts(line)}{index < lines.length - 1 && <br />}
  </React.Fragment>)}</div>;
}
