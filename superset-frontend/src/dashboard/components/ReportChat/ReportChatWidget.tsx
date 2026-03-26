/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
import { t } from '@apache-superset/core/translation';
import { styled, css } from '@apache-superset/core/theme';
import { Button, Flex, Input, Loading, Modal } from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import { SupersetClient } from '@superset-ui/core';
import { useEffect, useMemo, useState } from 'react';
import { useToasts } from 'src/components/MessageToasts/withToasts';

type ChatRole = 'user' | 'assistant';

type ChatMessage = {
  role: ChatRole;
  content: string;
};

type ReportChatWidgetProps = {
  dashboardId: string;
};

const CHAT_STORAGE_PREFIX = 'superset__report_chat__';
const MAX_MESSAGES = 30;

const FloatingButton = styled(Button)`
  position: fixed;
  right: ${({ theme }) => theme.sizeUnit * 6}px;
  bottom: ${({ theme }) => theme.sizeUnit * 6}px;
  z-index: 999;
  border-radius: ${({ theme }) => theme.borderRadius}px;
`;

const ChatTranscript = styled.div`
  padding: ${({ theme }) => theme.sizeUnit * 4}px;
  height: 55vh;
  overflow-y: auto;
`;

const ChatBubble = styled.div<{ $role: ChatRole }>`
  ${({ theme, $role }) => css`
    margin-bottom: ${theme.sizeUnit * 3}px;
    display: flex;
    flex-direction: column;
    align-items: ${$role === 'user' ? 'flex-end' : 'flex-start'};
  `}
`;

const BubbleLabel = styled.div`
  font-size: ${({ theme }) => theme.fontSizeSM}px;
  color: ${({ theme }) => theme.colorTextSecondary};
  margin-bottom: ${({ theme }) => theme.sizeUnit}px;
`;

const BubbleBody = styled.div<{ $role: ChatRole }>`
  ${({ theme, $role }) => css`
    max-width: 85%;
    white-space: pre-wrap;
    padding: ${theme.sizeUnit * 3}px ${theme.sizeUnit * 4}px;
    border-radius: ${theme.borderRadius}px;
    background: ${$role === 'user' ? theme.colorPrimaryBg : theme.colorBgContainer};
    color: ${$role === 'user' ? theme.colorPrimaryText : theme.colorText};
    border: 1px solid ${theme.colorBorderSecondary};
  `}
`;

const ChatFooter = styled.div`
  ${({ theme }) => css`
    display: flex;
    align-items: flex-end;
    gap: ${theme.sizeUnit * 3}px;
  `}
`;

export default function ReportChatWidget({
  dashboardId,
}: ReportChatWidgetProps) {
  const { addDangerToast } = useToasts();

  const storageKey = useMemo(
    () => `${CHAT_STORAGE_PREFIX}${dashboardId}`,
    [dashboardId],
  );

  const [isOpen, setIsOpen] = useState(false);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (!raw) return;
      const parsed = JSON.parse(raw) as { messages?: ChatMessage[] };
      if (Array.isArray(parsed.messages)) {
        setChatMessages(parsed.messages.slice(-MAX_MESSAGES));
      }
    } catch {
      // localStorage might be unavailable (incognito / blocked) - ignore.
    }
  }, [storageKey]);

  useEffect(() => {
    try {
      localStorage.setItem(
        storageKey,
        JSON.stringify({ messages: chatMessages }),
      );
    } catch {
      // ignore
    }
  }, [chatMessages, storageKey]);

  const canSend = draft.trim().length > 0 && !isLoading;

  const labelsByRole: Record<ChatRole, string> = useMemo(
    () => ({
      user: t('Bạn'),
      assistant: t('Trợ lý'),
    }),
    [],
  );

  const sendMessage = async () => {
    const content = draft.trim();
    if (!content || isLoading) return;

    setDraft('');
    const nextMessagesAll: ChatMessage[] = [
      ...chatMessages,
      { role: 'user', content },
    ];
    const nextMessages: ChatMessage[] = nextMessagesAll.slice(-MAX_MESSAGES);

    // Optimistic placeholder
    setChatMessages([
      ...nextMessages,
      { role: 'assistant', content: '' },
    ]);
    setIsLoading(true);

    try {
      const response = await SupersetClient.post({
        endpoint: '/api/v1/report_chat/message/',
        jsonPayload: {
          dashboardIdOrSlug: dashboardId,
          messages: nextMessages,
          max_slices: 3,
          chart_row_limit: 20,
          max_tokens: 700,
          temperature: 0.2,
        },
      });

      const assistantText =
        response?.result?.response ??
        response?.response ??
        response?.result ??
        '';

      setChatMessages(prev => {
        const updated = [...prev];
        const lastIdx = updated.length - 1;
        if (lastIdx >= 0) {
          updated[lastIdx] = {
            role: 'assistant',
            content: String(assistantText),
          };
        }
        return updated;
      });
    } catch (error) {
      addDangerToast(
        t('Không thể tạo phản hồi chatbot. Vui lòng thử lại.'),
      );
      setChatMessages(prev => {
        const updated = [...prev];
        const lastIdx = updated.length - 1;
        if (lastIdx >= 0 && updated[lastIdx]?.role === 'assistant') {
          updated[lastIdx] = {
            role: 'assistant',
            content: t('Có lỗi khi trả lời.'),
          };
        }
        return updated;
      });
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <>
      <FloatingButton
        buttonStyle="secondary"
        onClick={() => setIsOpen(true)}
        icon={<Icons.HistoryOutlined />}
        iconSize="m"
      >
        {t('Chat')}
      </FloatingButton>

      <Modal
        show={isOpen}
        onHide={() => setIsOpen(false)}
        title={t('Chatbot cho report')}
        width={650}
        footer={
          <ChatFooter>
            <Input.TextArea
              style={{ flex: 1 }}
              rows={2}
              value={draft}
              onChange={e => setDraft(e.currentTarget.value)}
              placeholder={t('Hỏi về dashboard...')}
              onKeyDown={e => {
                // Send on Ctrl/Cmd + Enter to avoid accidental sends.
                if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                  e.preventDefault();
                  void sendMessage();
                }
              }}
            />
            <Button
              buttonStyle="primary"
              onClick={() => void sendMessage()}
              disabled={!canSend}
            >
              {isLoading ? <Loading /> : t('Gửi')}
            </Button>
          </ChatFooter>
        }
      >
        <ChatTranscript>
          {chatMessages.length === 0 ? (
            <Flex justify="center" align="center" style={{ height: '100%' }}>
              <div>{t('Hỏi để chatbot phân tích dữ liệu trong dashboard.')}</div>
            </Flex>
          ) : (
            chatMessages.map((m, idx) => (
              <ChatBubble key={`${idx}-${m.role}`} $role={m.role}>
                <BubbleLabel>{labelsByRole[m.role]}</BubbleLabel>
                <BubbleBody $role={m.role}>
                  {m.content || (m.role === 'assistant' && isLoading ? '...' : '')}
                </BubbleBody>
              </ChatBubble>
            ))
          )}
        </ChatTranscript>
      </Modal>
    </>
  );
}

