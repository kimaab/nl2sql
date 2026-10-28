package com.nl2sql.web;

import java.util.List;
import java.util.Map;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import com.nl2sql.model.MetricModels.Relation;
import com.nl2sql.model.MetricModels.RelationInput;
import com.nl2sql.model.SystemModels.ColumnNoteInput;
import com.nl2sql.model.SystemModels.NeedsCalcInput;
import com.nl2sql.model.SystemModels.SyncLog;
import com.nl2sql.model.SystemModels.SyncResult;
import com.nl2sql.model.SystemModels.SystemInfo;
import com.nl2sql.model.SystemModels.SystemInput;
import com.nl2sql.model.SystemModels.TableInfo;
import com.nl2sql.model.SystemModels.TablePurposeInput;
import com.nl2sql.model.SystemModels.UtcOffsetInput;
import com.nl2sql.service.RelationService;
import com.nl2sql.service.SupersetService;
import com.nl2sql.service.SystemService;

/** 시스템 · 동기화 · 테이블 설정 · 관계 */
@RestController
public class SystemController {

    private final SystemService systems;
    private final RelationService relations;
    private final SupersetService superset;

    public SystemController(SystemService systems, RelationService relations, SupersetService superset) {
        this.systems = systems;
        this.relations = relations;
        this.superset = superset;
    }

    // ── 시스템 · 동기화 ─────────────────────────────────────────────────

    @GetMapping("/api/systems")
    public List<SystemInfo> getSystems() {
        return systems.listSystems();
    }

    @PostMapping("/api/systems")
    @ResponseStatus(HttpStatus.CREATED)
    public SystemInfo postSystem(@RequestBody SystemInput req) {
        return systems.createSystem(req.validated());
    }

    @GetMapping("/api/systems/{systemId}")
    public SystemInfo getSystemDetail(@PathVariable String systemId) {
        return systems.getSystem(Params.uuid(systemId));
    }

    @PutMapping("/api/systems/{systemId}")
    public SystemInfo putSystem(@PathVariable String systemId, @RequestBody SystemInput req) {
        return systems.updateSystem(Params.uuid(systemId), req.validated());
    }

    @DeleteMapping("/api/systems/{systemId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void deleteSystemDetail(@PathVariable String systemId) {
        systems.deleteSystem(Params.uuid(systemId));
    }

    @PostMapping("/api/systems/{systemId}/sync")
    public SyncResult postSync(@PathVariable String systemId) {
        return systems.syncSystem(Params.uuid(systemId));
    }

    /** 시스템을 Superset DB 연결로 등록한다 (이미 있으면 접속 정보 갱신). 데이터는 옮기지 않는다. */
    @PostMapping("/api/systems/{systemId}/superset")
    public SystemInfo postSuperset(@PathVariable String systemId) {
        return superset.linkDatabase(Params.uuid(systemId));
    }

    @GetMapping("/api/sync-logs")
    public List<SyncLog> getSyncLogs(@RequestParam(name = "system_id", required = false) String systemId,
            @RequestParam(defaultValue = "100") int limit) {
        return systems.listSyncLogs(Params.optionalUuid(systemId), Params.range("limit", limit, 1, 500));
    }

    @GetMapping("/api/systems/{systemId}/schema")
    public Map<String, Object> getSystemSchema(@PathVariable String systemId) {
        systems.getSystem(Params.uuid(systemId));
        return systems.getSchema(systemId);
    }

    // ── 테이블 설정 ──────────────────────────────────────────────────────

    @GetMapping("/api/systems/{systemId}/tables")
    public List<TableInfo> getTables(@PathVariable String systemId) {
        systems.getSystem(Params.uuid(systemId));
        return systems.listTables(systemId);
    }

    @PutMapping("/api/systems/{systemId}/tables/{tableId}/purpose")
    public TableInfo putTablePurpose(@PathVariable String systemId, @PathVariable String tableId,
            @RequestBody TablePurposeInput req) {
        return systems.setTablePurpose(Params.uuid(systemId), Params.uuid(tableId), req.purpose());
    }

    @PutMapping("/api/systems/{systemId}/tables/{tableId}/utc-offset")
    public TableInfo putUtcOffset(@PathVariable String systemId, @PathVariable String tableId, @RequestBody UtcOffsetInput req) {
        return systems.setUtcOffset(Params.uuid(systemId), Params.uuid(tableId), req.column(), req.hours());
    }

    @PutMapping("/api/systems/{systemId}/tables/{tableId}/needs-calc")
    public TableInfo putNeedsCalc(@PathVariable String systemId, @PathVariable String tableId, @RequestBody NeedsCalcInput req) {
        return systems.setNeedsCalc(Params.uuid(systemId), Params.uuid(tableId), req.column(), req.needsCalc());
    }

    @PutMapping("/api/systems/{systemId}/tables/{tableId}/column-note")
    public TableInfo putColumnNote(@PathVariable String systemId, @PathVariable String tableId, @RequestBody ColumnNoteInput req) {
        return systems.setColumnNote(Params.uuid(systemId), Params.uuid(tableId), req.column(), req.cleaned());
    }

    // ── 관계 ───────────────────────────────────────────────────────────

    @GetMapping("/api/systems/{systemId}/relations")
    public List<Relation> getRelations(@PathVariable String systemId) {
        systems.getSystem(Params.uuid(systemId));
        return relations.listRelations(systemId);
    }

    @PostMapping("/api/systems/{systemId}/relations")
    @ResponseStatus(HttpStatus.CREATED)
    public Relation postRelation(@PathVariable String systemId, @RequestBody RelationInput req) {
        systems.getSystem(Params.uuid(systemId));
        return relations.createRelation(systemId, req.validated());
    }

    @DeleteMapping("/api/systems/{systemId}/relations/{relationId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void deleteRelationDetail(@PathVariable String systemId, @PathVariable String relationId) {
        relations.deleteRelation(Params.uuid(systemId), Params.uuid(relationId));
    }
}
