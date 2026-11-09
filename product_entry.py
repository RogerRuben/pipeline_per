"""Frozen software entry: supervisor and workers share one sealed executable."""
import sys


def main():
    if '--worker' in sys.argv:
        sys.argv.remove('--worker')
        from product.worker import main as worker
        return worker()
    if '--model-check' in sys.argv:
        from pathlib import Path
        from product.common import write_json
        from services.price_service.ds_backend import load_price_service as PriceService
        index=sys.argv.index('--model-check')
        model,metadata,report=sys.argv[index+1:index+4]
        service=PriceService(Path(model),None,evaluation_metadata=Path(metadata))
        service.handle_post('/api/v1/predict',service.example_request())
        write_json(report,{'health':service.health(),'schema':service.schema()})
        return 0
    if '--target-check' in sys.argv:
        sys.argv.remove('--target-check')
        from product.target_check import main as target
        return target()
    from product.launcher import entry
    return entry()

if __name__ == '__main__':
    try:
        result=main()
    except Exception as exc:
        import traceback
        from product.common import write_json
        if '--model-check' in sys.argv:
            write_json(sys.argv[sys.argv.index('--model-check')+3],{'error':str(exc),'traceback':traceback.format_exc()})
        elif '--report' in sys.argv:
            write_json(sys.argv[sys.argv.index('--report')+1],{'status':'failed','error':str(exc)})
        if sys.stderr is not None:traceback.print_exc()
        result=1
    raise SystemExit(result)
