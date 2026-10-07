import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/Toast";
import { AskPage } from "./pages/AskPage";
import { SystemList } from "./pages/systems/SystemList";
import { SystemForm } from "./pages/systems/SystemForm";
import { SystemSync } from "./pages/systems/SystemSync";
import { SyncHistory } from "./pages/systems/SyncHistory";
import { TablesPage } from "./pages/systems/TablesPage";
import { RelationsPage } from "./pages/systems/RelationsPage";
import { ColumnDictionaryPage } from "./pages/systems/ColumnDictionaryPage";
import { BusinessGlossaryPage } from "./pages/systems/BusinessGlossaryPage";
import { HistoryPage } from "./pages/HistoryPage";
import { ReviewPage } from "./pages/ReviewPage";
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

          <Route path="/systems" element={<SystemList />} />
          <Route path="/systems/new" element={<SystemForm />} />
          <Route path="/systems/:id/edit" element={<SystemForm />} />
          <Route path="/sync" element={<SystemSync />} />
          <Route path="/sync/history" element={<SyncHistory />} />
          <Route path="/tables" element={<TablesPage />} />
          <Route path="/relations" element={<RelationsPage />} />
          <Route path="/dictionary" element={<ColumnDictionaryPage />} />
          <Route path="/glossary" element={<BusinessGlossaryPage />} />

          <Route path="/metrics" element={<MetricList />} />
          <Route path="/metrics/new" element={<MetricWizard />} />
          <Route path="/metrics/:dsId/:metricId" element={<MetricDetail />} />
          <Route path="/metrics/:dsId/:metricId/edit" element={<MetricWizard />} />

          <Route path="/review" element={<ReviewPage />} />

          <Route path="*" element={<Navigate to="/ask" replace />} />
        </Route>
      </Routes>
    </ToastProvider>
  );
}

export default App;
