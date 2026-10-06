import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/Toast";
import { AskPage } from "./pages/AskPage";
import { DatasourceList } from "./pages/datasources/DatasourceList";
import { DatasourceForm } from "./pages/datasources/DatasourceForm";
import { DatasourceSync } from "./pages/datasources/DatasourceSync";
import { SyncHistory } from "./pages/datasources/SyncHistory";
import { RelationsPage } from "./pages/datasources/RelationsPage";
import { GlossaryPage } from "./pages/datasources/GlossaryPage";
import { HistoryPage } from "./pages/HistoryPage";
import { MetricList } from "./pages/metrics/MetricList";
import { MetricDetail } from "./pages/metrics/MetricDetail";
import { MetricWizard } from "./pages/metrics/MetricWizard";
import "./styles.css";

function App() {
  return (
    <ToastProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Navigate to="/ask" replace />} />
          <Route path="/ask" element={<AskPage />} />
          <Route path="/history" element={<HistoryPage />} />

          <Route path="/datasources" element={<DatasourceList />} />
          <Route path="/datasources/new" element={<DatasourceForm />} />
          <Route path="/datasources/:id/edit" element={<DatasourceForm />} />
          <Route path="/sync" element={<DatasourceSync />} />
          <Route path="/sync/history" element={<SyncHistory />} />
          <Route path="/relations" element={<RelationsPage />} />
          <Route path="/glossary" element={<GlossaryPage />} />

          <Route path="/metrics" element={<MetricList />} />
          <Route path="/metrics/new" element={<MetricWizard />} />
          <Route path="/metrics/:dsId/:metricId" element={<MetricDetail />} />
          <Route path="/metrics/:dsId/:metricId/edit" element={<MetricWizard />} />

          <Route path="*" element={<Navigate to="/ask" replace />} />
        </Route>
      </Routes>
    </ToastProvider>
  );
}

export default App;
