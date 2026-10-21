"""Small synchronous canonical reports; capability required on every stored-report action."""
import os
from functools import lru_cache
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import Field
from app.api.auth import CurrentUser, get_current_user
from app.report_engine.models import Model, Operation
from app.report_engine.service import Engine
from app.report_engine.export import excel, pdf
from app.services.report_service import export_filename

router = APIRouter(prefix='/api/canonical-reports', tags=['Canonical reports'])

@lru_cache
def engine():
    database_url = os.getenv('DATABASE_URL', '').strip()
    if not database_url:
        raise HTTPException(503, 'Report persistence is unavailable.')
    return Engine(database_url)

class Create(Model):
    prompt: str = Field(default='Generate shipment analysis in graphical form', max_length=2000)
    records: list[dict] = Field(min_length=1, max_length=5000)
    source_label: str = Field(default='Application payload', max_length=200)


def capability(authorization: str = Header(default='')):
    if not authorization.startswith('Bearer ') or not authorization[7:]:
        raise HTTPException(401, 'Report bearer access token required')
    return authorization[7:]

@router.post('')
def create(request: Create, user: CurrentUser = Depends(get_current_user), service: Engine = Depends(engine)):
    # The initial intent classifier deliberately covers shipment analysis only.
    lowered = request.prompt.lower()
    unsupported = {'financial', 'revenue', 'profit', 'monthly', 'daily', 'state', 'date filter'}
    if any(word in lowered for word in unsupported):
        raise HTTPException(422, 'This slice supports shipment status and courier counts. Clarify the dimension or provide a supported request.')
    report, token = service.create(user.id, request.records, request.source_label)
    return {'report': report.model_dump(mode='json'), 'access_token': token,
            'plan': {'operation': 'CREATE_REPORT', 'skills': ['shipment-analysis', 'chart-management'],
                     'dimensions': ['status', 'courier'], 'narrative_source': 'deterministic'}}

@router.get('/{report_id}')
def preview(report_id: str, user: CurrentUser = Depends(get_current_user), token: str = Depends(capability), service: Engine = Depends(engine)):
    return service.get(report_id, user.id, token)

@router.post('/{report_id}/operations')
def operate(report_id: str, operation: Operation, user: CurrentUser = Depends(get_current_user), token: str = Depends(capability), service: Engine = Depends(engine)):
    return service.execute(report_id, user.id, token, operation)

@router.get('/{report_id}/export/{format}')
def export(report_id: str, format: Literal['xlsx', 'pdf'], version: int, user: CurrentUser = Depends(get_current_user), token: str = Depends(capability), service: Engine = Depends(engine)):
    report = service.get(report_id, user.id, token)
    if report.version != version:
        raise HTTPException(409, 'Report changed. Reload before exporting.')
    content = excel(report) if format == 'xlsx' else pdf(report)
    media = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if format == 'xlsx' else 'application/pdf'
    filename = export_filename(user.name, f'{report.title} v{version}', report.generated_at, format)
    return Response(content, media_type=media, headers={'Content-Disposition': f'attachment; filename="{filename}"', 'X-Report-Version': str(version)})
