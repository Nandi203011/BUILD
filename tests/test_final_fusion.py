from backend.schemas.independent_measurements import IndependentCVResult, IndependentMeasurement
from backend.schemas.vision import VisionDocumentResult, VisionPageResult, VisionDimension, VisionArea
from backend.spatial_reasoning.final_fusion import build_final_agreement


def test_final_fusion_agrees_on_independent_cv_and_vision():
    cv = IndependentCVResult(document_id='p', pages_analyzed=[1], measurements=[
        IndependentMeasurement(field='plot.width', value_m=12.19, source='NATIVE_TEXT', confidence=.99),
        IndependentMeasurement(field='plot.area', value=222.83, unit='m2', source='NATIVE_TEXT', confidence=.99),
    ])
    vision = VisionDocumentResult(model_name='test', pages=[VisionPageResult(page_number=1,
        dimensions=[VisionDimension(value=12.20, unit='m', type='PLOT_WIDTH', confidence=.95)],
        areas=[VisionArea(value=222.84, unit='m2', type='PLOT_AREA', confidence=.95)])])
    out = build_final_agreement(cv, vision)
    assert out['values']['plot.width']['status'] == 'AGREED'
    assert out['values']['plot.area']['status'] == 'AGREED'


def test_final_fusion_does_not_pick_a_winner_on_conflict():
    cv = IndependentCVResult(document_id='p', measurements=[
        IndependentMeasurement(field='plot.width', value_m=12.19, source='NATIVE_TEXT', confidence=.99)])
    vision = VisionDocumentResult(model_name='test', pages=[VisionPageResult(page_number=1,
        dimensions=[VisionDimension(value=10.0, unit='m', type='PLOT_WIDTH', confidence=.99)])])
    out = build_final_agreement(cv, vision)
    assert out['values']['plot.width']['status'] == 'CONFLICT'
    assert out['values']['plot.width']['value'] is None
