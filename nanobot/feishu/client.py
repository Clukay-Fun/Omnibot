"""Thin wrapper around the Feishu SDK client."""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from nanobot.config.schema import FeishuConfig


class FeishuClient:
    """Encapsulate Feishu SDK calls so the channel stays thin."""

    _FILE_TYPE_MAP = {
        ".opus": "opus",
        ".mp4": "mp4",
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "doc",
        ".xls": "xls",
        ".xlsx": "xls",
        ".ppt": "ppt",
        ".pptx": "ppt",
    }

    def __init__(self, sdk_client: Any):
        self.sdk_client = sdk_client

    @classmethod
    def build(cls, config: FeishuConfig) -> "FeishuClient":
        import lark_oapi as lark

        sdk_client = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        return cls(sdk_client)

    @staticmethod
    def resolve_receive_id_type(receive_id: str) -> str:
        return "chat_id" if receive_id.startswith("oc_") else "open_id"

    def upload_image_sync(self, file_path: str) -> str | None:
        """Upload an image to Feishu and return the image_key."""
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        try:
            with open(file_path, "rb") as file_obj:
                request = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(file_obj)
                        .build()
                    )
                    .build()
                )
                response = self.sdk_client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    logger.debug("Uploaded image {}: {}", os.path.basename(file_path), image_key)
                    return image_key
                logger.error("Failed to upload image: code={}, msg={}", response.code, response.msg)
                return None
        except Exception as e:
            logger.error("Error uploading image {}: {}", file_path, e)
            return None

    def upload_file_sync(self, file_path: str) -> str | None:
        """Upload a file to Feishu and return the file_key."""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as file_obj:
                request = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(file_obj)
                        .build()
                    )
                    .build()
                )
                response = self.sdk_client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    logger.debug("Uploaded file {}: {}", file_name, file_key)
                    return file_key
                logger.error("Failed to upload file: code={}, msg={}", response.code, response.msg)
                return None
        except Exception as e:
            logger.error("Error uploading file {}: {}", file_path, e)
            return None

    def download_image_sync(self, message_id: str, image_key: str) -> tuple[bytes | None, str | None]:
        """Download an image from Feishu message by message_id and image_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            response = self.sdk_client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            logger.error("Failed to download image: code={}, msg={}", response.code, response.msg)
            return None, None
        except Exception as e:
            logger.error("Error downloading image {}: {}", image_key, e)
            return None, None

    def download_file_sync(
        self,
        message_id: str,
        file_key: str,
        resource_type: str = "file",
    ) -> tuple[bytes | None, str | None]:
        """Download a file/audio/media from a Feishu message by message_id and file_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        if resource_type == "audio":
            resource_type = "file"

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self.sdk_client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            logger.error("Failed to download {}: code={}, msg={}", resource_type, response.code, response.msg)
            return None, None
        except Exception:
            logger.exception("Error downloading {} {}", resource_type, file_key)
            return None, None

    def reply_message_sync(
        self,
        message_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        """Reply to a Feishu message and return the new message_id."""
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        try:
            request = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type(msg_type)
                    .content(content)
                    .reply_in_thread(reply_in_thread)
                    .build()
                )
                .build()
            )
            response = self.sdk_client.im.v1.message.reply(request)
            if not response.success():
                logger.error(
                    "Failed to reply with Feishu {} message: code={}, msg={}, log_id={}",
                    msg_type,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return None
            replied_id = getattr(response.data, "message_id", None)
            logger.debug("Feishu {} reply created for source {}", msg_type, message_id)
            return str(replied_id) if replied_id else None
        except Exception as e:
            logger.error("Error replying with Feishu {} message: {}", msg_type, e)
            return None

    def _create_or_reply_message_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: str,
        *,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        """Create a new message or reply to an existing message."""
        if reply_to:
            return self.reply_message_sync(
                reply_to,
                msg_type,
                content,
                reply_in_thread=reply_in_thread,
            )

        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self.sdk_client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    "Failed to create Feishu {} message: code={}, msg={}, log_id={}",
                    msg_type,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return None
            message_id = getattr(response.data, "message_id", None)
            logger.debug("Feishu {} message created for {}", msg_type, receive_id)
            return str(message_id) if message_id else None
        except Exception as e:
            logger.error("Error creating Feishu {} message: {}", msg_type, e)
            return None

    def patch_message_sync(self, message_id: str, msg_type: str, content: str) -> bool:
        """Patch an existing Feishu message."""
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        try:
            request = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self.sdk_client.im.v1.message.patch(request)
            if not response.success():
                logger.error(
                    "Failed to patch Feishu {} message {}: code={}, msg={}, log_id={}",
                    msg_type,
                    message_id,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return False
            logger.debug("Patched Feishu {} message {}", msg_type, message_id)
            return True
        except Exception as e:
            logger.error("Error patching Feishu {} message {}: {}", msg_type, message_id, e)
            return False

    def delete_message_sync(self, message_id: str) -> bool:
        """Delete an existing Feishu message."""
        from lark_oapi.api.im.v1 import DeleteMessageRequest

        try:
            request = DeleteMessageRequest.builder().message_id(message_id).build()
            response = self.sdk_client.im.v1.message.delete(request)
            if not response.success():
                logger.error(
                    "Failed to delete Feishu message {}: code={}, msg={}, log_id={}",
                    message_id,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return False
            logger.debug("Deleted Feishu message {}", message_id)
            return True
        except Exception as e:
            logger.error("Error deleting Feishu message {}: {}", message_id, e)
            return False

    def create_message_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: str,
        reply_to: str | None = None,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        """Create a Feishu message and return its message_id."""
        return self._create_or_reply_message_sync(
            receive_id_type,
            receive_id,
            msg_type,
            content,
            reply_to=reply_to,
            reply_in_thread=reply_in_thread,
        )

    def list_users_sync(
        self,
        *,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> tuple[list[Any], str | None, bool]:
        """List Feishu users and return items plus pagination state."""
        from lark_oapi.api.contact.v3 import ListUserRequest

        try:
            builder = (
                ListUserRequest.builder()
                .user_id_type("open_id")
                .page_size(page_size)
            )
            if page_token:
                builder = builder.page_token(page_token)
            request = builder.build()
            response = self.sdk_client.contact.v3.user.list(request)
            if not response.success():
                logger.error(
                    "Failed to list Feishu users: code={}, msg={}, log_id={}",
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return [], None, False

            data = getattr(response, "data", None)
            items = list(getattr(data, "items", None) or [])
            next_page_token = getattr(data, "page_token", None) or None
            has_more = bool(getattr(data, "has_more", False))
            return items, next_page_token, has_more
        except Exception as e:
            logger.error("Error listing Feishu users: {}", e)
            return [], None, False

    def send_message_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: str,
        reply_to: str | None = None,
        *,
        reply_in_thread: bool = False,
    ) -> bool:
        """Send a single message (text/image/file/interactive) synchronously."""
        return (
            self.create_message_sync(
                receive_id_type,
                receive_id,
                msg_type,
                content,
                reply_to,
                reply_in_thread=reply_in_thread,
            )
            is not None
        )
