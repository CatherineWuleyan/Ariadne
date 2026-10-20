"""Built-in automated paper-formalization pipeline declaration."""

from ..models import JSONDict


AUTOMATED_FORMALIZATION_PIPELINE: JSONDict = {
    "pipeline_id": "automated-paper-formalization",
    "version": "3.0.0",
    "stages": [
        {"name": "import-real-inputs", "plugin": "pipeline_harness.real_inputs:RealInputImporter", "options": {}},
        {"name": "step1_extract_evidence", "plugin": "pipeline_harness.domain.stages:Step1ExtractEvidencePlugin", "options": {}},
        {"name": "step2_normalize_claims", "plugin": "pipeline_harness.domain.stages:Step2NormalizeClaimsPlugin", "options": {"tool_plugin": "pipeline_harness.domain.vision:DeepSeekFlashVisionTool"}},
        {"name": "step3_analyze_reasoning", "plugin": "pipeline_harness.domain.stages:Step3AnalyzeReasoningPlugin", "options": {}},
        {"name": "step4_formalize_reasoning", "plugin": "pipeline_harness.domain.stages:Step4FormalizeReasoningPlugin", "options": {}},
        {"name": "step5_compile_gaia_ir", "plugin": "pipeline_harness.domain.compiler:Step5CompileGaiaIRPlugin", "options": {}},
    ],
    "view_adapter": "pipeline_harness.domain.view:FormalizationViewAdapter",
}
