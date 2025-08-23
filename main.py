import os
import asyncio
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from uuid import uuid4
import shutil
import zipfile
from datetime import datetime

# Import the correct functions from your checker files
from Fastcheck import process_file_and_check as fast_check
from Slowcheck import process_file_and_check as slow_check  
from Logout import process_file_and_check as logout_check

# --- Config ---
TOKEN = "7311871048:AAGZur8okUq1SufTT-i6wwtoz15ZC0fq0BU"  # Replace with your actual bot token
UPLOAD_DIR = "uploads"

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

# --- Helper: Create ZIP file from valid cookies ---
def create_results_zip(results_dir, mode, original_filename):
    """Create a ZIP file containing all valid cookies"""
    valid_dir = os.path.join(results_dir, 'valid_cookies')
    
    if not os.path.exists(valid_dir) or not os.listdir(valid_dir):
        return None
    
    # Create zip filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = os.path.splitext(original_filename)[0]
    zip_filename = f"{base_name}_{mode}_valid_{timestamp}.zip"
    zip_path = os.path.join(results_dir, zip_filename)
    
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for filename in os.listdir(valid_dir):
                file_path = os.path.join(valid_dir, filename)
                if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                    zipf.write(file_path, filename)
        
        # Check if zip file was created and has content
        if os.path.exists(zip_path) and os.path.getsize(zip_path) > 0:
            return zip_path
        else:
            return None
            
    except Exception as e:
        print(f"Error creating ZIP file: {e}")
        return None

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
🕐 ETA: {eta_str if eta > 0 else '--'}

✅ Valid: {valid}
❌ Invalid: {invalid}"""
    
    return status_msg

# --- Helper: Process file with specific mode (REVISED for live updates) ---
async def process_file_with_mode(update, file_path, file_name, mode, reply_to_message=None):
    """Process a file with the specified checking mode"""
    
    # Send initial processing message
    if reply_to_message:
        status_msg = await reply_to_message.reply_text(
            f"🔄 **Starting {mode.upper()} check...**\n"
            f"📁 File: `{file_name}`\n"
            f"⏳ Initializing...",
            parse_mode='Markdown'
        )
    else:
        status_msg = await update.message.reply_text(
            f"🔄 **Starting {mode.upper()} check...**\n"
            f"📁 File: `{file_name}`\n"
            f"⏳ Initializing...",
            parse_mode='Markdown'
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
                    elapsed = asyncio.get_event_loop().time() - start_time
                    status_text = format_processing_status(
                        progress["checked"],
                        progress["total"],
                        progress["valid"],
                        progress["invalid"],
                        elapsed,
                        mode
                    )
                    await status_msg.edit_text(status_text, parse_mode='Markdown')
                    await asyncio.sleep(2)
                except Exception:
                    break

        status_task = asyncio.create_task(update_status())

        # --- wrapper to run checkers that yield progress ---
        def run_check(func, file_path):
            nonlocal results_dir
            for step in func(file_path, live=True):
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
            return results_dir

        # Run actual check depending on mode
        if mode == 'fast':
            results_dir = await loop.run_in_executor(None, run_check, fast_check, file_path)
        elif mode == 'slow':
            results_dir = await loop.run_in_executor(None, run_check, slow_check, file_path)
        elif mode == 'logout':
            results_dir = await loop.run_in_executor(None, run_check, logout_check, file_path)

        # Stop updater
        status_task.cancel()

        # --- continue original logic (sending results, cleanup, etc.) ---
        sent_files = 0
        total_size = 0
        
        if results_dir and os.path.exists(results_dir):
            # Check for valid cookies directory
            valid_dir = os.path.join(results_dir, 'valid_cookies')
            if os.path.exists(valid_dir):
                valid_files = [f for f in os.listdir(valid_dir) 
                             if os.path.isfile(os.path.join(valid_dir, f)) and 
                             os.path.getsize(os.path.join(valid_dir, f)) > 0]
                
                if valid_files:
                    # Determine if original was an archive or single file
                    file_ext = os.path.splitext(file_name)[1].lower()
                    is_archive = file_ext in ['.zip', '.rar']
                    
                    if is_archive or len(valid_files) > 3:
                        # Create ZIP file for archives or when there are many files
                        zip_path = create_results_zip(results_dir, mode, file_name)
                        
                        if zip_path:
                            zip_size = os.path.getsize(zip_path)
                            with open(zip_path, 'rb') as f:
                                await update.message.reply_document(
                                    document=f,
                                    filename=os.path.basename(zip_path)
                                )
                            sent_files = 1
                            total_size = zip_size
                                
                        else:
                            await update.message.reply_text("❌ Error creating results archive.")
                    
                    else:
                        # Send individual files for single file uploads with few results
                        for filename in valid_files:
                            file_path_result = os.path.join(valid_dir, filename)
                            try:
                                file_size = os.path.getsize(file_path_result)
                                with open(file_path_result, 'rb') as f:
                                    await update.message.reply_document(
                                        document=f,
                                        filename=filename
                                    )
                                sent_files += 1
                                total_size += file_size
                            except Exception as e:
                                print(f"Error sending file {filename}: {e}")

        # Delete the processing status message to keep conversation clean
        try:
            await status_msg.delete()
        except Exception:
            pass

        # Send summary as a temporary message if no valid files found
        if sent_files == 0:
            summary_msg = await update.message.reply_text(
                f"❌ **No valid cookies found**\n"
                f"📁 File: `{file_name}`\n"
                f"🔍 Mode: {mode.upper()} check",
                parse_mode='Markdown'
            )
            # Delete summary after 10 seconds
            await asyncio.sleep(10)
            try:
                await summary_msg.delete()
            except Exception:
                pass

        # Clean up result directory
        if results_dir and os.path.exists(results_dir):
            cleanup_directory(results_dir)
            
        # Clean up uploaded file
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Error cleaning up uploaded file: {e}")

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

# --- Command: /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = """Raven NF Checker**
    
Send a file, then reply to it with:
• `/fastcheck` - Quick validation
• `/slowcheck` - Thorough validation  
• `/logout` - Logout check
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
            await process_file_with_mode(update, file_path, doc.file_name, mode)
            
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

# --- Main Application ---
def main():
    print("🤖 Initializing Netflix Cookie Checker Bot...")
    
    app = ApplicationBuilder().token(TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fastcheck", fastcheck))
    app.add_handler(CommandHandler("slowcheck", slowcheck))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    
    # Add error handler
    app.add_error_handler(error_handler)

    print("✅ Bot is running and ready to receive files...")
    print("🔍 Features:")
    print("   • Auto-detection from filename")
    print("   • Reply to files with commands")
    print("   • Reply to results for rechecking")
    print("   • Clean conversation mode")
    print("🗂️ Archive support: ZIP/RAR files supported")
    
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
    except Exception as e:
        print(f"❌ Bot crashed: {e}")

if __name__ == '__main__':
    main()

