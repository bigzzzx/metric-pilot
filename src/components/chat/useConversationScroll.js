import { useLayoutEffect, useRef } from 'react';

// Desktop scrolls <main>; the narrow layout scrolls the document instead.
function scrollContainer(element) {
  const main = element.closest('main');
  return main && /auto|scroll/.test(getComputedStyle(main).overflowY)
    ? main : document.scrollingElement;
}

export default function useConversationScroll(session, messages) {
  const pageRef = useRef(null);
  const following = useRef(true);
  const moveToLatest = useRef(() => {});

  useLayoutEffect(() => {
    const page = pageRef.current;
    if (!page) return;
    following.current = true;
    const move = () => {
      if (!following.current) return;
      const container = scrollContainer(page);
      // Immediate positioning avoids animating through years of old messages.
      container.scrollTop = page.querySelector('.conversation-list') ? container.scrollHeight : 0;
    };
    const onScroll = event => {
      const container = scrollContainer(page);
      // Ignore nested tables, sidebars and other independently scrolling areas.
      if (event.target !== container && !(container === document.scrollingElement && event.target === document)) return;
      following.current = container.scrollHeight - container.scrollTop - container.clientHeight <= 64;
    };
    moveToLatest.current = move;
    document.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', move);
    // Covers streamed text, tables and other content whose height changes later.
    const observer = new ResizeObserver(move);
    observer.observe(page);
    move();
    return () => {
      observer.disconnect();
      document.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', move);
      moveToLatest.current = () => {};
    };
  }, [session]);

  useLayoutEffect(() => { moveToLatest.current(); }, [messages]);

  const followLatest = () => { following.current = true; };
  return { pageRef, followLatest };
}
