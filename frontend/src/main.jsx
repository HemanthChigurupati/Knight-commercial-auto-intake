// Two screens, chosen by the URL hash: #/ (queue) and #/s/<id> (one submission).
import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { api } from "./api.js";
import Queue from "./Queue.jsx";
import Submission from "./Submission.jsx";

function useRoute() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => { setHash(window.location.hash); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const m = hash.match(/^#\/s\/([\w-]+)/);
  return m ? { page: "submission", id: m[1] } : { page: "queue" };
}

function App() {
  const route = useRoute();
  const [config, setConfig] = useState(null);
  useEffect(() => { api.config().then(setConfig).catch(() => setConfig({ mode: "unknown" })); }, []);
  return (
    <>
      <div className="topbar">
        <a href="#/" className="brand">Knight Intake</a>
        <span className="brand-sub">underwriting file</span>
        {config && (config.mode === "demo" ? (
          <span className="mode" title="Every AI output shown was recorded from a live run on Amazon Bedrock. New uploads run the steps that need no AI.">Recorded Bedrock run</span>
        ) : (
          <span className="mode mode-live">Live: {config.mode}</span>
        ))}
      </div>
      {route.page === "submission" ? <Submission id={route.id} /> : <Queue config={config} />}
    </>
  );
}

createRoot(document.getElementById("root")).render(<App />);
