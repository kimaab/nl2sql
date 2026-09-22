import { useState } from "react";
import { DatasourcePage } from "./DatasourcePage";
import { AskPage } from "./AskPage";
import { MetricsPage } from "./MetricsPage";
import "./App.css";

function App() {
  const [currentPage, setCurrentPage] = useState<"datasource" | "metrics" | "ask">("ask");

  return (
    <div className="app">
      <header className="header">
        <h1>nl2sql Studio</h1>
        <nav>
          <button
            className={currentPage === "ask" ? "active" : ""}
            onClick={() => setCurrentPage("ask")}
          >
            질문
          </button>
          <button
            className={currentPage === "datasource" ? "active" : ""}
            onClick={() => setCurrentPage("datasource")}
          >
            데이터소스
          </button>
          <button
            className={currentPage === "metrics" ? "active" : ""}
            onClick={() => setCurrentPage("metrics")}
          >
            지표
          </button>
        </nav>
      </header>

      <main>
        {currentPage === "ask" && <AskPage />}
        {currentPage === "datasource" && <DatasourcePage />}
        {currentPage === "metrics" && <MetricsPage />}
      </main>
    </div>
  );
}

export default App;
