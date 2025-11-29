from datetime import datetime
from typing import List, Dict, Any

from fastapi import APIRouter, Query

router = APIRouter()


@router.get("/api/posts")
async def list_posts(limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    # כרגע פוסטים דמיוניים  רק כדי שפיד לא יקרוס
    example_posts = [
        {
            "id": "demo-1",
            "author": "SLHNET System",
            "title": "ברוכים הבאים ל-SLHNET",
            "content": "פוסט דמו ראשוני. הפוסטים האמיתיים יגיעו דרך הבוט /post.",
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
    ]
    return {
        "items": example_posts[:limit],
        "total": len(example_posts),
    }
