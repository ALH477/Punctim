# Copyright (C) 2025 DeMoD LLC
#
# This file is part of the Punctim LLM Interface System.
#
# The Punctim LLM Interface System is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# The Punctim LLM Interface System is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.

FROM rust:1.80 AS streamdb-builder
WORKDIR /app
COPY src/rust/ .
RUN cargo build --release
RUN cp target/release/libstreamdb.so /app/

FROM python:3.12-slim
WORKDIR /app
COPY src/python/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/python/ .
COPY src/proto/dcf.proto .
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. dcf.proto
COPY --from=streamdb-builder /app/libstreamdb.so /usr/local/lib/
ENV LD_LIBRARY_PATH=/usr/local/lib
CMD ["python", "llm_hydra_interface.py"]
