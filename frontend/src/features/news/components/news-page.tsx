const frameStyle = {
  height: "calc(100vh - 5.25rem)",
  minHeight: "760px",
};

export function NewsPage() {
  return (
    <iframe
      title="News"
      src="/news-frame"
      style={frameStyle}
      className="block w-full rounded-[2rem] border-0 bg-background shadow-[0_18px_80px_rgba(0,0,0,0.24)]"
    />
  );
}
