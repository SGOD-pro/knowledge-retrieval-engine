import { useState } from "react"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import * as z from "zod"
import { Upload, FileType, CheckCircle2 } from "lucide-react"
import { toast } from "sonner"
import { NotLiveBadge } from "@/components/NotLiveBadge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Form, FormControl, FormField, FormItem, FormMessage } from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"

const MAX_FILE_SIZE = 50 * 1024 * 1024 // 50MB
const ALLOWED_FORMATS = [".pdf", ".docx", ".xlsx", ".pptx", ".csv"]

const formSchema = z.object({
  file: z.any()
    .refine((files) => files?.length === 1, "File is required.")
    .refine((files) => files?.[0]?.size <= MAX_FILE_SIZE, "Max file size is 50MB.")
    .refine((files) => {
      if (!files?.[0]) return false
      const name = files[0].name.toLowerCase()
      return ALLOWED_FORMATS.some(ext => name.endsWith(ext))
    }, `Only ${ALLOWED_FORMATS.join(", ")} formats are supported.`),
})

interface UploadedDocument {
  id: string
  filename: string
  source_format: string
  chunk_count: number
  status: "processing" | "ingested"
}

export function Library() {
  const [isUploading, setIsUploading] = useState(false)
  const [documents, setDocuments] = useState<UploadedDocument[]>([])

  const form = useForm<z.infer<typeof formSchema>>({
    resolver: zodResolver(formSchema),
  })

  async function onSubmit(values: z.infer<typeof formSchema>) {
    const file = values.file[0]
    setIsUploading(true)

    try {
      const formData = new FormData()
      formData.append("file", file)

      // Proxy in vite.config.ts should route this to backend
      const res = await fetch("/api/ingest", {
        method: "POST",
        body: formData,
      })

      if (!res.ok) {
        throw new Error("Upload failed")
      }

      const data = await res.json()
      toast.success("Document uploaded successfully")
      
      setDocuments(prev => [
        {
          id: data.id,
          filename: data.filename,
          source_format: data.source_format,
          chunk_count: data.chunk_count,
          status: "ingested"
        },
        ...prev
      ])
      
      form.reset()
    } catch (error) {
      console.error(error)
      toast.error("Failed to upload document")
    } finally {
      setIsUploading(false)
    }
  }

  return (
    <div className="p-6 h-full flex flex-col gap-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight text-primary">Document Library</h2>
          <p className="text-muted-foreground">Manage your ingested knowledge base.</p>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Upload Document</CardTitle>
          <CardDescription>
            Supported formats: {ALLOWED_FORMATS.join(", ")}. Max size: 50MB.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
              <FormField
                control={form.control}
                name="file"
                render={({ field: { onChange, value: _value, ...field } }) => (
                  <FormItem>
                    <FormControl>
                      <div className="flex items-center justify-center w-full">
                        <label className="flex flex-col items-center justify-center w-full h-32 border-2 border-dashed rounded-lg cursor-pointer hover:bg-muted/50 border-muted-foreground/25">
                          <div className="flex flex-col items-center justify-center pt-5 pb-6">
                            <Upload className="w-8 h-8 mb-3 text-muted-foreground" />
                            <p className="mb-2 text-sm text-muted-foreground">
                              <span className="font-semibold">Click to upload</span> or drag and drop
                            </p>
                          </div>
                          <Input 
                            {...field}
                            type="file" 
                            className="hidden" 
                            onChange={(e) => onChange(e.target.files)}
                            accept={ALLOWED_FORMATS.join(",")}
                          />
                        </label>
                      </div>
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <Button type="submit" disabled={isUploading} className="w-full">
                {isUploading ? (
                  <>
                    <Upload className="mr-2 h-4 w-4 animate-bounce" />
                    Uploading...
                  </>
                ) : (
                  <>
                    <Upload className="mr-2 h-4 w-4" />
                    Upload
                  </>
                )}
              </Button>
            </form>
          </Form>
        </CardContent>
      </Card>

      <div className="space-y-4">
        <h3 className="text-lg font-medium">Recent Documents</h3>
        <NotLiveBadge text="GET /documents is not built yet / only available when live. Showing optimistic session state." />
        
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Filename</TableHead>
                <TableHead>Format</TableHead>
                <TableHead>Chunks</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {documents.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={4} className="text-center text-muted-foreground h-24">
                    No documents uploaded in this session.
                  </TableCell>
                </TableRow>
              ) : (
                documents.map((doc) => (
                  <TableRow key={doc.id}>
                    <TableCell className="font-medium">{doc.filename}</TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <FileType className="h-4 w-4 text-muted-foreground" />
                        <span className="uppercase text-xs">{doc.source_format}</span>
                      </div>
                    </TableCell>
                    <TableCell>{doc.chunk_count}</TableCell>
                    <TableCell>
                      <Badge variant="outline" className="bg-primary/10 text-primary border-primary/20">
                        <CheckCircle2 className="mr-1 h-3 w-3" />
                        Ingested
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  )
}
