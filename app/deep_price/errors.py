class DeepPriceError(Exception):
    def __init__(self, code, message, status=400):
        Exception.__init__(self, message)
        self.code, self.status = code, status

    def response(self, request_id):
        return {"success": False, "request_id": request_id,
                "error": {"code": self.code, "message": str(self), "details": {}}}
