"""Page bounds for compact catalog metadata, independent of content reads."""

from typing import Annotated

from pydantic import Field, StrictInt

CatalogLimit = Annotated[StrictInt, Field(ge=1, le=20)]
