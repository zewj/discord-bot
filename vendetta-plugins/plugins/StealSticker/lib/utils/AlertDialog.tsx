import { React } from "@vendetta/metro/common";
import { findByProps } from "@vendetta/metro";
import { showConfirmationAlert, showInputAlert } from "@vendetta/ui/alerts";
import { showToast } from "@vendetta/ui/toasts";

const { openAlert } = findByProps("openAlert", "dismissAlert") ?? {};
const { AlertModal, AlertActionButton } = findByProps("AlertModal", "AlertActions") ?? {};
const { Stack, TextInput } = findByProps("Stack") ?? {};

export interface InputDialogOptions {
    title: string;
    content?: string;
    initialValue?: string;
    placeholder?: string;
    confirmText: string;
    cancelText?: string;
    allowEmpty?: boolean;
    onConfirm: (value: string) => void;
}

export function showInputDialog(opts: InputDialogOptions) {
    if (!AlertModal || !AlertActionButton) {
        showInputAlert({
            title: opts.title,
            placeholder: opts.placeholder,
            confirmText: opts.confirmText,
            cancelText: opts.cancelText ?? "Cancel",
            onConfirm: opts.onConfirm,
        });
        return;
    }

    const key = `stealsticker-${opts.title?.toLowerCase().replaceAll(" ", "-")}`;
    openAlert(key, <InputDialog {...opts} />);
}

function InputDialog({
    title, content, initialValue, placeholder, confirmText, cancelText, allowEmpty, onConfirm,
}: InputDialogOptions) {
    const [value, setValue] = React.useState(initialValue ?? "");

    const submit = () => {
        if (!allowEmpty && !value.trim().length) return showToast("Name can't be blank");
        onConfirm(value);
    };

    return (
        <AlertModal
            title={title}
            content={content}
            extraContent={
                <TextInput
                    isClearable
                    value={value}
                    onChange={setValue}
                    placeholder={placeholder}
                    returnKeyType="done"
                    onSubmitEditing={submit}
                />
            }
            actions={
                <Stack>
                    <AlertActionButton
                        disabled={!allowEmpty && !value.trim().length}
                        text={confirmText}
                        variant="primary"
                        onPress={submit}
                    />
                    {cancelText ? <AlertActionButton text={cancelText} variant="secondary" /> : null}
                </Stack>
            }
        />
    );
}

export { showConfirmationAlert };
